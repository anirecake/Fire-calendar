"""FireCalendar API: harmonized MODIS+VIIRS burning-activity calendar.

Run locally:  uvicorn app.main:app --reload --port 8000      (docs at /docs)

Area of interest (every POST endpoint takes the same body, all fields optional):
  {"country": "Kazakhstan"}                                   FIRMS country name
  {"geometry": {GeoJSON Polygon | MultiPolygon}}              drawn area (lon, lat)
  {"lat": 50.4, "lon": 80.2, "radius_km": 25}                 point + radius
  + optional "start" / "end" (YYYY-MM-DD) for analytics and exports
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import shapely
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "processed"
WEB = ROOT / "web"

MAX_VERTICES = 5000
MAX_CSV_ROWS = 100_000          # keeps exports under serverless response limits (gzip'd)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

app = FastAPI(
    title="FireCalendar Central Asia API",
    description="Harmonized MODIS + VIIRS active-fire hotspots (NASA FIRMS) as a burning-activity calendar.",
    version="1.1",
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

con = duckdb.connect(config={"home_directory": tempfile.gettempdir()})  # serverless: read-only $HOME
# Views over parquet: no load at cold start, low memory; DuckDB scans only the columns it needs.
con.execute(f"CREATE VIEW pts AS SELECT * FROM read_parquet('{(DATA / 'points.parquet').as_posix()}')")
# expected VIIRS-only small fires for pre-VIIRS cell-months where MODIS saw nothing (cell centres)
con.execute(f"CREATE VIEW fill AS SELECT * FROM read_parquet('{(DATA / 'fill.parquet').as_posix()}')")
META: dict[str, Any] = json.loads((DATA / "model.json").read_text(encoding="utf-8"))
# which site this is (Central Asia or United States): names, map view, quick places, emergency number, AI context
DOMAIN: dict[str, Any] = (json.loads((DATA / "domain.json").read_text(encoding="utf-8")) if (DATA / "domain.json").exists()
                          else {"id": "ca", "name": {"en": "Central Asia"}, "emergency_number": "112", "alerts": True,
                                "bbox": [46.0, 35.0, 88.0, 56.0], "i18n": {},
                                "ai_region": "Central Asia (Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan, Uzbekistan)",
                                "ai_people": "the provincial emergency service and local authorities", "ai_causes": ""})
COUNTRIES = [r[0] for r in con.execute("SELECT DISTINCT country FROM pts ORDER BY 1").fetchall()]
# admin-1 regions, fire events, 14-day risk (optional files: the app still runs without them)
REGIONS = {f["properties"]["id"]: f["properties"] for f in
           json.loads((WEB / "regions.geojson").read_text(encoding="utf-8"))["features"]} if (WEB / "regions.geojson").exists() else {}
HAS_EVENTS = (DATA / "events.parquet").exists()
if HAS_EVENTS:
    con.execute(f"CREATE VIEW ev AS SELECT * FROM read_parquet('{(DATA / 'events.parquet').as_posix()}')")
EVENTS_META = json.loads((DATA / "events_meta.json").read_text(encoding="utf-8")) if (DATA / "events_meta.json").exists() else {}
RISK = json.loads((DATA / "risk.json").read_text(encoding="utf-8")) if (DATA / "risk.json").exists() else None
RISK_CELLS = json.loads((DATA / "risk_cells.json").read_text(encoding="utf-8")) if (DATA / "risk_cells.json").exists() else None
FIRST_DATE = pd.Timestamp(META["date_range"][0])
LAST_DATE = pd.Timestamp(META["date_range"][1])
FIRST_YEAR = FIRST_DATE.year
YEARS = list(range(FIRST_YEAR, LAST_DATE.year + 1))
VIIRS_START = pd.Timestamp(META["viirs_start"])
AQUA_START = pd.Timestamp(META["aqua_start"])
RECENT_FROM = pd.Timestamp(META["recent_from"]) if META.get("recent_from") else None
UNC = META["uncertainty"]


# --------------------------------------------------------------------------- input
class Area(BaseModel):
    geometry: dict | None = Field(None, description="GeoJSON Polygon or MultiPolygon, lon/lat")
    country: str | None = Field(None, description="FIRMS country name, see /api/meta")
    region: str | None = Field(None, description="admin-1 region id, see /api/regions (e.g. KAZ-3206)")
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    radius_km: float | None = Field(None, gt=0, le=1000)
    start: str | None = Field(None, description="YYYY-MM-DD")
    end: str | None = Field(None, description="YYYY-MM-DD")

    @field_validator("start", "end")
    @classmethod
    def _date(cls, v):
        if v in (None, ""):
            return None
        try:
            return str(dt.date.fromisoformat(v))
        except ValueError:
            raise ValueError("dates must be YYYY-MM-DD")

    def key(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True)


def circle(lat: float, lon: float, r_km: float, n: int = 64) -> shapely.Polygon:
    """Circle as a polygon in lon/lat (local equirectangular approximation, fine up to ~1000 km)."""
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    dlat = r_km / 111.32
    dlon = r_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return shapely.Polygon(np.c_[lon + dlon * np.cos(a), lat + dlat * np.sin(a)])


def attr_where(area: Area) -> tuple[list[str], list]:
    """SQL filters for country / region (tables pts, fill and ev all carry both columns)."""
    where, params = [], []
    if area.country:
        where.append("country = ?"); params.append(area.country)
    if area.region:
        where.append("region = ?"); params.append(area.region)
    return where, params


def resolve(area: Area) -> tuple[str | None, shapely.Geometry | None]:
    if area.country and area.country not in COUNTRIES:
        raise HTTPException(400, f"unknown country '{area.country}'; use one of {COUNTRIES}")
    if area.region and area.region not in REGIONS:
        raise HTTPException(400, f"unknown region '{area.region}', see /api/regions")
    geom = None
    if area.lat is not None or area.lon is not None or area.radius_km is not None:
        if None in (area.lat, area.lon, area.radius_km):
            raise HTTPException(400, "point search needs lat, lon and radius_km")
        geom = circle(area.lat, area.lon, area.radius_km)
    elif area.geometry:
        try:
            geom = shapely.geometry.shape(area.geometry)
        except Exception as e:
            raise HTTPException(400, f"bad geometry: {e}")
        if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            raise HTTPException(400, "geometry must be a non-empty Polygon or MultiPolygon")
        if shapely.get_num_coordinates(geom) > MAX_VERTICES:
            raise HTTPException(400, f"geometry has more than {MAX_VERTICES} vertices")
        if not geom.is_valid:
            geom = shapely.make_valid(geom)
        w, s, e, n = geom.bounds
        if w < -180 or e > 180 or s < -90 or n > 90:
            raise HTTPException(400, "coordinates must be lon/lat in degrees")
    return area.country, geom


# --------------------------------------------------------------------------- queries
def select_points(area: Area, cols: str, extra_where: str = "", params_extra=(), table: str = "pts"):
    """Rows of `table` inside the area (bbox in SQL, exact polygon test in shapely)."""
    _, geom = resolve(area)
    where, params = attr_where(area)
    if geom is not None:
        w, s, e, n = geom.bounds
        where.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"); params += [w, e, s, n]
    if extra_where:
        where.append(extra_where); params += list(params_extra)
    sql = f"SELECT {cols} FROM {table}" + (" WHERE " + " AND ".join(where) if where else "")
    df = con.cursor().execute(sql, params).df()   # cursor = own connection per request (thread-safe)
    if geom is not None and len(df) and not geom.equals(shapely.box(*geom.bounds)):
        shapely.prepare(geom)
        df = df[shapely.contains_xy(geom, df.lon.to_numpy(), df.lat.to_numpy())]
    return df


def grouped(area: Area, keys: list[str], table: str = "pts") -> pd.DataFrame:
    """sum(w) and count per `keys` inside the area (DuckDB aggregation unless a polygon test is needed)."""
    expr = {"day": "day(date) AS day", "doy": "dayofyear(date) AS doy"}
    k = ", ".join(expr.get(c, c) for c in keys)
    _, geom = resolve(area)
    if geom is None:
        where, params = attr_where(area)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return con.cursor().execute(
            f"SELECT {k}, sum(w) AS w, count(*) AS n FROM {table} {w} GROUP BY ALL", params).df()
    pts = select_points(area, f"lat, lon, w, {k}", table=table)
    return pts.groupby(keys, as_index=False).agg(w=("w", "sum"), n=("w", "size"))


def matrix(df: pd.DataFrame, value: pd.Series) -> np.ndarray:
    m = np.zeros((len(YEARS), 12))
    if len(df):
        for (y, mo), v in value.groupby([df.year, df.month]).sum().items():
            m[y - FIRST_YEAR, mo - 1] = v
    return m


def complete_mask() -> np.ndarray:
    """Months fully covered by the record."""
    mask = np.zeros((len(YEARS), 12), bool)
    for i, y in enumerate(YEARS):
        for mo in range(12):
            start = pd.Timestamp(y, mo + 1, 1)
            mask[i, mo] = start >= FIRST_DATE.replace(day=1) and start + pd.offsets.MonthEnd(0) <= LAST_DATE
    return mask


OK = complete_mask()


def source_matrix() -> list[list[str | None]]:
    """What each month is made of: modis_terra (Terra only, rescaled), modis (rescaled),
    mixed (rescaled MODIS until VIIRS starts mid-month, Jan 2012), viirs, recent (API)."""
    out = []
    for y in YEARS:
        row = []
        for mo in range(1, 13):
            start = pd.Timestamp(y, mo, 1)
            end = start + pd.offsets.MonthEnd(0)
            if not OK[y - FIRST_YEAR, mo - 1]:
                row.append(None)
            elif end < AQUA_START:
                row.append("modis_terra")
            elif end < VIIRS_START:
                row.append("modis")
            elif start < VIIRS_START:
                row.append("mixed")
            elif RECENT_FROM is not None and end >= RECENT_FROM:
                row.append("recent")
            else:
                row.append("viirs")
        out.append(row)
    return out


SOURCES = source_matrix()


def half_width(modeled: np.ndarray, recent: np.ndarray) -> np.ndarray:
    """90% interval half-width for harmonized totals.
    modeled: rescaled-MODIS part (pre-VIIRS) - calibrated two-part error model (see model.json)
    recent : current-year API part - ~5% systematic (country + static-mask back-test)."""
    return np.sqrt(UNC["z"] ** 2 * UNC["phi"] * modeled + (1.645 * UNC["sigma_sys"] * modeled) ** 2
                   + (1.645 * META.get("recent_sigma", 0.05) * recent) ** 2)


def _round(a, nd=1):
    if isinstance(a, np.ndarray) and a.ndim == 2:
        return [[None if np.isnan(v) else round(float(v), nd) for v in row] for row in a]
    return [None if (v is None or np.isnan(v)) else round(float(v), nd) for v in a]


# --------------------------------------------------------------------------- core computation
@lru_cache(maxsize=256)
def _cube(key: str) -> dict:
    """Everything computed per area, cached (areas repeat a lot: countries, demo polygons)."""
    area = Area(**json.loads(key))
    df = grouped(area, ["year", "month", "day", "doy", "sensor"])
    fill = grouped(area, ["year", "month"], table="fill")
    is_v = df.sensor == "VIIRS"
    pts_w = matrix(df, df.w)
    fill_w = matrix(fill, fill.w)
    harm = pts_w + fill_w
    modeled = matrix(df[~is_v], df.w[~is_v]) + fill_w
    recent = np.zeros_like(harm)
    if RECENT_FROM is not None:
        rv = df[is_v & (df.year >= RECENT_FROM.year)]
        recent = matrix(rv, rv.w)
    return {
        "df": df, "fill": fill, "harm": harm, "modeled": modeled, "recent": recent,
        "raw_modis": matrix(df[~is_v], df.n[~is_v]), "raw_viirs": matrix(df[is_v], df.n[is_v]),
    }


def cube(area: Area) -> dict:
    resolve(area)            # validate before hitting the cache
    a = area.model_copy(update={"start": None, "end": None})
    return _cube(a.key())


# --------------------------------------------------------------------------- statistics
def mann_kendall(y: np.ndarray) -> dict | None:
    """Mann-Kendall trend test + Sen's slope (no external deps)."""
    y = np.asarray(y, float)
    n = len(y)
    if n < 8:
        return None
    i, j = np.triu_indices(n, 1)
    d = y[j] - y[i]
    s = np.sign(d).sum()
    _, counts = np.unique(y, return_counts=True)
    var = (n * (n - 1) * (2 * n + 5) - (counts * (counts - 1) * (2 * counts + 5)).sum()) / 18
    z = 0.0 if s == 0 else (s - np.sign(s)) / math.sqrt(var)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    slope = float(np.median(d / (j - i)))
    mean = float(y.mean())
    return {
        "n_years": n, "sen_slope_per_year": round(slope, 1),
        "pct_per_decade": round(slope * 10 / mean * 100, 1) if mean > 0 else None,
        "mk_z": round(float(z), 2), "p_value": round(p, 4),
        "direction": "decreasing" if slope < 0 else "increasing" if slope > 0 else "no trend",
        "significant_95": bool(p < 0.05),
    }


def fire_season(profile: np.ndarray, share: float = 0.8) -> dict | None:
    """Shortest run of consecutive months (wrapping over New Year) holding `share` of the year's fires."""
    tot = profile.sum()
    if tot <= 0:
        return None
    p = profile / tot
    best = None
    for length in range(1, 13):
        for s in range(12):
            idx = [(s + k) % 12 for k in range(length)]
            if p[idx].sum() >= share:
                cand = (length, -p[idx].sum(), s)
                best = cand if best is None or cand < best else best
        if best:
            break
    length, neg, s = best
    peak = int(np.argmax(p))
    risk = ["high" if v >= 0.5 * p.max() else "elevated" if v >= 0.2 * p.max() else "low" for v in p]
    return {
        "start_month": s + 1, "end_month": (s + length - 1) % 12 + 1, "length_months": length,
        "share": round(-neg, 3), "peak_month": peak + 1,
        "monthly_share": [round(float(v), 4) for v in p], "risk": risk,
        "label": f"{MONTHS[s]}–{MONTHS[(s + length - 1) % 12]}",
    }


def daily_cumulative(c: dict) -> dict:
    """Cumulative harmonized hotspots by day of year for every year (fill spread evenly over its month)."""
    df, fill = c["df"], c["fill"]
    daily = np.zeros((len(YEARS), 367))
    for (y, doy), v in df.w.groupby([df.year, df.doy]).sum().items():
        daily[y - FIRST_YEAR, doy] += v
    for r in fill.itertuples():
        start = pd.Timestamp(r.year, r.month, 1)
        dim = start.days_in_month
        d0 = start.day_of_year
        daily[r.year - FIRST_YEAR, d0:d0 + dim] += r.w / dim
    return {"cum": np.cumsum(daily[:, 1:], axis=1)}


# --------------------------------------------------------------------------- endpoints
@app.get("/api/meta", summary="Dataset metadata, model coefficients, validation")
def meta():
    return {**META, "countries": COUNTRIES, "max_csv_rows": MAX_CSV_ROWS, "domain": DOMAIN,
            "events": {"available": HAS_EVENTS, **({k: EVENTS_META[k] for k in ("events", "by_type", "rules")} if EVENTS_META else {})},
            "risk": {"available": bool(RISK), **({"issue_date": RISK["issue_date"], "horizon_days": RISK["horizon_days"],
                                                   "validation": RISK["validation"], "definition": RISK["definition"]} if RISK else {})}}


@app.get("/api/health", summary="Liveness check")
def health():
    return {"ok": True, "last_date": str(LAST_DATE.date()), "cache": _cube.cache_info()._asdict()}


@app.post("/api/calendar", summary="Year x month harmonized calendar with anomalies and 90% intervals")
def calendar(area: Area):
    c = cube(area)
    harm, df, fill = c["harm"], c["df"], c["fill"]
    h = np.where(OK, harm, np.nan)
    median = np.nanmedian(h, axis=0)
    mean = np.nanmean(h, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        anomaly = np.where(median > 0, (h - median) / median * 100, np.where(h > 0, 100.0, 0.0))
    anomaly = np.where(OK, anomaly, np.nan)
    half = np.where(OK, half_width(c["modeled"], c["recent"]), np.nan)

    annual = np.nansum(np.where(OK, harm, 0), axis=1)
    full_years = [i for i in range(len(YEARS)) if OK[i].all()]
    top_years = sorted(full_years, key=lambda i: -annual[i])[:3]
    last_full = LAST_DATE if LAST_DATE.is_month_end else LAST_DATE - pd.offsets.MonthEnd(1)
    li, lm = last_full.year - FIRST_YEAR, last_full.month - 1

    # month to date: current partial month vs the same days of that month in past years
    mtd = None
    if not LAST_DATE.is_month_end:
        M, D, cy = LAST_DATE.month, LAST_DATE.day, LAST_DATE.year
        pm = df[(df.month == M) & (df.day <= D)]
        per_year = pm.w.groupby(pm.year).sum()
        fm = fill[fill.month == M]
        fill_y = fm.w.groupby(fm.year).sum()
        past = []
        for i, y in enumerate(YEARS):
            if y == cy or not OK[i, M - 1]:
                continue
            past.append(per_year.get(y, 0.0) + fill_y.get(y, 0.0) * D / pd.Timestamp(y, M, 1).days_in_month)
        cur = float(per_year.get(cy, 0.0))
        med = float(np.median(past)) if past else float("nan")
        mtd = {"year": cy, "month": M, "day": D, "value": round(cur, 1), "median": round(med, 1),
               "p10": round(float(np.percentile(past, 10)), 1) if past else None,
               "p90": round(float(np.percentile(past, 90)), 1) if past else None,
               "anomaly_pct": round((cur - med) / med * 100, 1) if med > 0 else None,
               "years_compared": len(past)}

    return {
        "years": YEARS,
        "harmonized": _round(h),
        "harmonized_lo": _round(np.maximum(h - half, 0)),
        "harmonized_hi": _round(h + half),
        "source": SOURCES,
        "raw_modis": _round(np.where(OK, c["raw_modis"], np.nan)),
        "raw_viirs": _round(np.where(OK, c["raw_viirs"], np.nan)),
        "anomaly_pct": _round(anomaly),
        "monthly_median": _round(median),
        "monthly_mean": _round(mean),
        "monthly_p10": _round(np.nanpercentile(h, 10, axis=0)),
        "monthly_p90": _round(np.nanpercentile(h, 90, axis=0)),
        "annual": _round(annual),
        "stats": {
            "points": int(df.n.sum()) if len(df) else 0,
            "peak_month": int(np.nanargmax(median)) + 1 if np.nansum(median) else None,
            "top_years": [{"year": YEARS[i], "hotspots": round(float(annual[i]))} for i in top_years],
            "month_to_date": mtd,
            "latest": {
                "year": int(last_full.year), "month": int(lm + 1),
                "value": round(float(harm[li, lm]), 1), "median": round(float(median[lm]), 1),
                "anomaly_pct": None if np.isnan(anomaly[li, lm]) else round(float(anomaly[li, lm]), 1),
            },
        },
    }


def period(area: Area) -> tuple[pd.Timestamp, pd.Timestamp]:
    s = max(pd.Timestamp(area.start) if area.start else FIRST_DATE, FIRST_DATE)
    e = min(pd.Timestamp(area.end) if area.end else LAST_DATE, LAST_DATE)
    if s > e:
        raise HTTPException(400, f"empty period: data cover {FIRST_DATE.date()}..{LAST_DATE.date()}")
    return s, e


@app.post("/api/analytics", summary="Analytics for an area and date range: totals, trend, fire season, year-to-date")
def analytics(area: Area):
    c = cube(area)
    s, e = period(area)
    harm, modeled, recent = c["harm"], c["modeled"], c["recent"]

    # totals over the exact date range (monthly values prorated at the edges)
    total = mod_part = rec_part = 0.0
    months = pd.period_range(s, e, freq="M")
    df, fill = c["df"], c["fill"]
    for pm in months:
        yi, mi = pm.year - FIRST_YEAR, pm.month - 1
        m_start, m_end = pm.start_time.normalize(), pm.end_time.normalize()
        a, b = max(m_start, s), min(m_end, e)
        if a == m_start and b == m_end:
            total += harm[yi, mi]; mod_part += modeled[yi, mi]; rec_part += recent[yi, mi]
            continue
        sel = df[(df.year == pm.year) & (df.month == pm.month) & (df.day >= a.day) & (df.day <= b.day)]
        fw = fill[(fill.year == pm.year) & (fill.month == pm.month)].w.sum() * ((b - a).days + 1) / pm.days_in_month
        total += sel.w.sum() + fw
        mod_part += sel[sel.sensor == "MODIS"].w.sum() + fw
        if RECENT_FROM is not None and pm.year >= RECENT_FROM.year:
            rec_part += sel[sel.sensor == "VIIRS"].w.sum()
    hw = float(half_width(np.array(mod_part), np.array(rec_part)))

    # full calendar years inside the period -> trend and season
    yrs = [y for y in YEARS if pd.Timestamp(y, 1, 1) >= s and pd.Timestamp(y, 12, 31) <= e and OK[y - FIRST_YEAR].all()]
    yi = [y - FIRST_YEAR for y in yrs]
    annual = harm[yi].sum(axis=1) if yi else np.array([])
    viirs_yrs = [y for y in yrs if y >= VIIRS_START.year + 1]
    viirs_annual = c["raw_viirs"][[y - FIRST_YEAR for y in viirs_yrs]].sum(axis=1) if viirs_yrs else np.array([])
    profile = np.median(harm[yi], axis=0) if yi else np.zeros(12)

    # year-to-date: current (or last) year vs percentiles of complete years in the period
    cum = daily_cumulative(c)["cum"]
    ref = [y - FIRST_YEAR for y in yrs if y != LAST_DATE.year]
    cur_year = min(e.year, LAST_DATE.year)
    upto = (min(e, LAST_DATE) - pd.Timestamp(cur_year, 1, 1)).days + 1 if cur_year == e.year else 366
    ytd = None
    if ref:
        pct = np.percentile(cum[ref], [10, 50, 90], axis=0)
        cur = cum[cur_year - FIRST_YEAR][:upto]
        ytd = {
            "year": cur_year, "through_doy": int(upto),
            "current": _round(cur, 0), "p10": _round(pct[0], 0), "p50": _round(pct[1], 0), "p90": _round(pct[2], 0),
            "current_vs_median_pct": round((cur[-1] - pct[1][upto - 1]) / pct[1][upto - 1] * 100, 1)
            if pct[1][upto - 1] > 0 else None,
            "reference_years": [YEARS[i] for i in ref],
        }

    return {
        "period": {"start": str(s.date()), "end": str(e.date())},
        "total": {"harmonized_hotspots": round(total, 1), "lo90": round(max(total - hw, 0), 1),
                  "hi90": round(total + hw, 1), "rescaled_modis_share": round(mod_part / total, 3) if total else 0.0},
        "annual": [{"year": y, "hotspots": round(float(v), 1)} for y, v in zip(yrs, annual)],
        "trend": mann_kendall(annual),
        "trend_viirs_only": mann_kendall(viirs_annual),
        "fire_season": fire_season(profile),
        "ytd": ytd,
        "notes": [
            "trend: Mann-Kendall test + Sen's slope on harmonized annual totals of full years in the period",
            "trend_viirs_only: same test on raw VIIRS (2013+), i.e. what you would conclude without the MODIS record",
            "fire_season: shortest run of consecutive months holding 80% of the median year's hotspots",
        ],
    }


def _csv(df: pd.DataFrame, name: str) -> Response:
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.post("/api/calendar.csv", summary="Monthly harmonized series for the area as CSV (with 90% interval)")
def calendar_csv(area: Area):
    cal = calendar(area)
    s, e = period(area)
    rows = []
    for yi, y in enumerate(cal["years"]):
        for mi in range(12):
            v = cal["harmonized"][yi][mi]
            if v is None or not (s.to_period("M") <= pd.Period(year=y, month=mi + 1, freq="M") <= e.to_period("M")):
                continue
            rows.append({"year": y, "month": mi + 1, "harmonized_hotspots": v,
                         "lo90": cal["harmonized_lo"][yi][mi], "hi90": cal["harmonized_hi"][yi][mi],
                         "source": cal["source"][yi][mi], "raw_modis": cal["raw_modis"][yi][mi],
                         "raw_viirs": cal["raw_viirs"][yi][mi], "median_same_month": cal["monthly_median"][mi],
                         "anomaly_pct": cal["anomaly_pct"][yi][mi]})
    return _csv(pd.DataFrame(rows), "fire_calendar.csv")


@app.post("/api/hotspots.csv", summary="Raw filtered hotspots in the area and date range (max 100k rows)")
def hotspots_csv(area: Area):
    s, e = period(area)
    df = select_points(area, "lat, lon, date, time, sensor, satellite, conf, frp, daynight, country, w",
                       "date BETWEEN ? AND ?", (s.date(), e.date()))
    if len(df) > MAX_CSV_ROWS:
        raise HTTPException(413, f"{len(df):,} hotspots match; narrow the area or dates to ≤ {MAX_CSV_ROWS:,}")
    df = df.rename(columns={"lat": "latitude", "lon": "longitude", "w": "harmonized_weight"}).sort_values("date")
    return _csv(df, "hotspots.csv")


@lru_cache(maxsize=256)
def _grid(year: int | None, month: int | None, region: str | None = None, country: str | None = None,
          bbox: tuple[float, float, float, float] | None = None) -> dict:
    """Harmonized hotspots per cell. With region / country / bbox only that area is returned, so the map
    loads just what is selected (a province is ~1-2% of the Central Asia grid)."""
    where, params = ["w > 0"], []
    if year:
        where.append("year = ?"); params.append(year)
    if month:
        where.append("month = ?"); params.append(month)
    if region:
        where.append("region = ?"); params.append(region)
    if country:
        where.append("country = ?"); params.append(country)
    if bbox:
        where.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"); params += [bbox[0], bbox[2], bbox[1], bbox[3]]
    span = 1.0 if year else (LAST_DATE - FIRST_DATE).days / 365.25
    w_sql = " AND ".join(where)
    rows = con.cursor().execute(f"""
        SELECT cx, cy, sum(w) / {span} AS v FROM (
            SELECT cx, cy, w FROM pts WHERE {w_sql}
            UNION ALL
            SELECT cx, cy, w FROM fill WHERE {w_sql}
        ) GROUP BY ALL
    """, params * 2).fetchall()
    cd = META["cell_deg"]
    return {"cell": cd, "year": year, "month": month, "region": region, "country": country,
            "cells": [[round(cy * cd, 4), round(cx * cd, 4), round(v, 2)] for cx, cy, v in rows]}


@app.get("/api/grid", summary="Harmonized hotspots per 0.25° cell for the map (per year average if no filter); "
                              "optional region / country / bbox=w,s,e,n to load only the selected area")
def grid(year: int | None = None, month: int | None = None, region: str | None = None,
         country: str | None = None, bbox: str | None = None):
    if year is not None and year not in YEARS:
        raise HTTPException(400, f"year must be in {YEARS[0]}..{YEARS[-1]}")
    if month is not None and not 1 <= month <= 12:
        raise HTTPException(400, "month must be 1..12")
    if region is not None and region not in REGIONS:
        raise HTTPException(400, f"unknown region '{region}'")
    if country is not None and country not in COUNTRIES:
        raise HTTPException(400, f"unknown country '{country}'")
    box = None
    if bbox:
        try:
            box = tuple(round(float(x), 2) for x in bbox.split(","))      # rounded: better cache reuse
        except ValueError:
            raise HTTPException(400, "bbox must be w,s,e,n")
        if len(box) != 4 or not (box[0] < box[2] and box[1] < box[3]):
            raise HTTPException(400, "bbox must be w,s,e,n with w<e and s<n")
    return _grid(year, month, region, country, box)


# --------------------------------------------------------------------------- regions, events, risk
@lru_cache(maxsize=1)
def _region_stats() -> dict:
    """Per-region headline numbers for the analyst / emergency views (computed once)."""
    d = con.cursor().execute("SELECT region, year, month, day(date) AS day, sum(w) AS w FROM pts GROUP BY ALL").df()
    fl = con.cursor().execute("SELECT region, year, month, sum(w) AS w FROM fill GROUP BY ALL").df()
    full = [y for y in YEARS if OK[y - FIRST_YEAR].all()]
    last_full = LAST_DATE if LAST_DATE.is_month_end else LAST_DATE - pd.offsets.MonthEnd(1)
    out = {}
    ev_stats = {}
    if HAS_EVENTS:
        since = LAST_DATE.year - 4
        e = con.cursor().execute(f"""
            SELECT region, count(*) n, count(*) FILTER (WHERE type='large') large,
                   count(*) FILTER (WHERE type='recurrent') recurrent, sum(area_km2) area
            FROM ev WHERE year >= {since} GROUP BY 1""").df().set_index("region")
        big = con.cursor().execute("""
            SELECT region, arg_max(id, area_km2) id, max(area_km2) area, arg_max(start, area_km2) start FROM ev GROUP BY 1
        """).df().set_index("region")
        ev_stats = {"recent": e, "biggest": big, "since": since}
    for rid in REGIONS:
        rd, rf = d[d.region == rid], fl[fl.region == rid]
        m = np.zeros((len(YEARS), 12))
        for (y, mo), v in rd.groupby(["year", "month"]).w.sum().items():
            m[y - FIRST_YEAR, mo - 1] += v
        for (y, mo), v in rf.groupby(["year", "month"]).w.sum().items():
            m[y - FIRST_YEAR, mo - 1] += v
        annual = np.array([m[y - FIRST_YEAR].sum() for y in full])
        mm = np.where(OK, m, np.nan)
        med = np.nanmedian(mm, axis=0)
        li, lm = last_full.year - FIRST_YEAR, last_full.month - 1
        last_anom = (m[li, lm] - med[lm]) / med[lm] * 100 if med[lm] > 0 else None
        tr = mann_kendall(annual)
        rec = {
            "avg_per_year": round(float(annual.mean()), 1),
            "last5_per_year": round(float(annual[-5:].mean()), 1),
            "trend_pct_decade": tr["pct_per_decade"] if tr else None,
            "trend_p": tr["p_value"] if tr else None,
            "last_month": {"year": last_full.year, "month": last_full.month, "value": round(float(m[li, lm]), 1),
                           "median": round(float(med[lm]), 1),
                           "anomaly_pct": None if last_anom is None else round(float(last_anom), 1)},
            "peak_month": int(np.nanargmax(med)) + 1 if np.nansum(med) > 0 else None,
        }
        if ev_stats:
            r = ev_stats["recent"]
            if rid in r.index:
                n = int(r.at[rid, "n"])
                rec["events_recent"] = {"since": ev_stats["since"], "n": n, "large": int(r.at[rid, "large"]),
                                        "recurrent_share": round(float(r.at[rid, "recurrent"] / n), 3) if n else 0}
            b = ev_stats["biggest"]
            if rid in b.index:
                rec["biggest_event"] = {"id": int(b.at[rid, "id"]), "area_km2": float(b.at[rid, "area"]), "start": str(b.at[rid, "start"])}
        if RISK and rid in RISK["regions"]:
            rk = RISK["regions"][rid]
            rec["risk"] = {"prob": rk["prob"], "level": rk["level"], "times_usual": rk["times_usual"]}
        out[rid] = rec
    return out


@app.get("/api/regions", summary="Admin-1 regions with headline numbers (fires/year, trend, last month, events, 14-day risk)")
def regions():
    st = _region_stats()
    return {"regions": [{"id": rid, **{k: p[k] for k in ("country", "en", "ru", "kk", "city")}, **st.get(rid, {})}
                        for rid, p in REGIONS.items()],
            "risk_issue_date": RISK["issue_date"] if RISK else None,
            "note": "Natural Earth admin-1 boundaries (Kazakhstan before the 2022 reform: no Abai, Jetisu, Ulytau)"}


class EventQuery(Area):
    types: list[str] | None = Field(None, description="large | recurrent | small")
    min_area_km2: float = Field(0, ge=0)
    intense_only: bool = False
    since: str | None = Field(None, description="events still burning on/after this date (YYYY-MM-DD)")
    sort: str = Field("area_km2", pattern="^(area_km2|frp_max_mw|frp_sum_mw|duration_days|spread_km_day|start|end|detections|random)$")
    desc: bool = True
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)

    @field_validator("since")
    @classmethod
    def _since(cls, v):
        if v in (None, ""):
            return None
        try:
            return str(dt.date.fromisoformat(v))
        except ValueError:
            raise ValueError("since must be YYYY-MM-DD")


EVENT_COLS = ("id, start, \"end\", duration_days, detections, area_km2, frp_max_mw, frp_sum_mw, night_share, "
              "extent_km, spread_km_day, recurrence, type, intense, lat, lon, region, country, year, month")


@app.post("/api/events", summary="Fire events (clustered VIIRS hotspots) with properties: filter, sort, summarize")
def events(q: EventQuery):
    if not HAS_EVENTS:
        raise HTTPException(404, "events not built")
    _, geom = resolve(q)
    s, e = period(q)
    where, params = attr_where(q)
    where += ["\"end\" >= ?", "start <= ?", "area_km2 >= ?"]
    params += [s.date(), e.date(), q.min_area_km2]
    if q.since:
        where.append("\"end\" >= ?"); params.append(dt.date.fromisoformat(q.since))
    if q.types:
        bad = set(q.types) - {"large", "recurrent", "small"}
        if bad:
            raise HTTPException(400, f"unknown types {bad}")
        where.append(f"type IN ({','.join('?' * len(q.types))})"); params += q.types
    if q.intense_only:
        where.append("intense")
    if geom is not None:
        w_, s_, e_, n_ = geom.bounds
        where.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"); params += [w_, e_, s_, n_]
    df = con.cursor().execute(f"SELECT {EVENT_COLS} FROM ev WHERE {' AND '.join(where)}", params).df()
    if geom is not None and len(df):
        shapely.prepare(geom)
        df = df[shapely.contains_xy(geom, df.lon.to_numpy(), df.lat.to_numpy())]
    by_year = (df.groupby(["year", "type"]).agg(n=("id", "size"), area=("area_km2", "sum")).reset_index()
               if len(df) else pd.DataFrame(columns=["year", "type", "n", "area"]))
    if q.sort == "random":                         # reproducible sample (e.g. for scatter plots)
        page = df.sample(n=min(q.limit, len(df)), random_state=0) if len(df) else df.copy()
    else:
        page = df.sort_values(q.sort, ascending=not q.desc).iloc[q.offset:q.offset + q.limit].copy()
    for c in ("start", "end"):
        page[c] = page[c].astype(str)
    page = page.replace({np.nan: None})
    return {
        "total": int(len(df)),
        "by_type": {t: int(n) for t, n in df.type.value_counts().items()},
        "area_km2_total": round(float(df.area_km2.sum()), 1),
        "by_year": [{"year": int(r.year), "type": r.type, "n": int(r.n), "area_km2": round(float(r.area), 1)} for r in by_year.itertuples()],
        "events": page.to_dict("records"),
        "rules": EVENTS_META.get("rules"),
    }


@app.get("/api/risk", summary="14-day fire-activity risk per region: probability, drivers, weather now, past analogs, validation")
def risk():
    if not RISK:
        raise HTTPException(404, "risk model not built")
    return {**RISK, "names": {rid: {k: p[k] for k in ("en", "ru", "kk", "country")} for rid, p in REGIONS.items()}}


@app.get("/api/risk/cells", summary="Place-level 14-day fire probability (~25 km cells) for one province or one country")
def risk_cells(region: str | None = None, country: str | None = None):
    if not RISK_CELLS:
        raise HTTPException(404, "cell risk model not built")
    if region:
        if region not in REGIONS:
            raise HTTPException(400, f"unknown region '{region}'")
        ids = [region]
    elif country:
        if country not in COUNTRIES:
            raise HTTPException(400, f"unknown country '{country}'")
        ids = [rid for rid, p in REGIONS.items() if p["country"] == country]
    else:
        raise HTTPException(400, "give region or country")
    cols = RISK_CELLS["columns"]
    cells = [{**dict(zip(cols, c)), "region": rid} for rid in ids for c in RISK_CELLS["regions"].get(rid, [])]
    cells.sort(key=lambda c: -c["prob"])
    return {"region": region, "country": country, "issue_date": RISK_CELLS["issue_date"], "cell_deg": RISK_CELLS["cell_deg"],
            "definition": RISK_CELLS["definition"], "levels": RISK_CELLS["levels"], "validation": RISK_CELLS["validation"],
            "cells": cells}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "internal error", "type": type(exc).__name__})


from app.assistant import router as assistant_router  # noqa: E402  (after the core is defined)

app.include_router(assistant_router)
from app.push import router as push_router  # noqa: E402

app.include_router(push_router)
from app.briefing import router as briefing_router  # noqa: E402

app.include_router(briefing_router)


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Service worker for browser push alerts; served from the root so its scope covers the whole site."""
    return FileResponse(WEB / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


def _env(name: str) -> str | None:
    import os
    v = os.environ.get(name, "").strip()
    env = ROOT / ".env"
    if not v and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return v or None


TELEGRAM_BOT = (_env("TELEGRAM_BOT_USERNAME") or "").lstrip("@") or None


@app.get("/api/links", summary="Optional companion services (Telegram alert bot)")
def links():
    return {"telegram_bot": TELEGRAM_BOT}


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
