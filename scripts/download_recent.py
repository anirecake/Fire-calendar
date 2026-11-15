"""Download current-year hotspots (not yet in the yearly archive) from the FIRMS API.

Needs a free FIRMS MAP_KEY (https://firms.modaps.eosdis.nasa.gov/api/map_key/).
Put it in `fire-calendar/.env` as   FIRMS_MAP_KEY=xxxxxxxx   (file is git-ignored),
or set the FIRMS_MAP_KEY environment variable.

What it does
  * asks FIRMS which dates each source covers (data_availability)
  * per sensor: Standard Processing (SP) up to its last date, Near Real-Time (NRT) after
  * queries the Central Asia bounding box in 5-day windows (API maximum)
  * assigns each hotspot a country from the 2000-2025 archive (0.1 deg cell majority,
    3x3 neighbourhood fallback); points outside the five countries are dropped
  * NRT has no `type` field, so static sources (gas flares, industry, volcanoes) are
    removed with a mask of locations flagged type 1/2 in the archive
  * writes data/raw/<sensor>/<sensor>_<year>_<Country>.csv in the archive format,
    so build_dataset.py picks them up unchanged

Usage:  python scripts/download_recent.py [--year 2026]
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import time
import urllib.request
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_firms import CENTRAL_ASIA, CTX, RAW, ROOT  # noqa: E402
import domain  # noqa: E402

API = "https://firms.modaps.eosdis.nasa.gov/api"
BBOX = domain.BBOX_STR         # west,south,east,north of the current domain
WINDOW = 5                     # max DAY_RANGE allowed by the area API
SENSORS = {                    # archive folder -> (SP source, NRT source)
    "modis": ("MODIS_SP", "MODIS_NRT"),
    "viirs-snpp": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
}
COLUMNS = {
    "modis": ["latitude", "longitude", "brightness", "scan", "track", "acq_date", "acq_time", "satellite",
              "instrument", "confidence", "version", "bright_t31", "frp", "daynight", "type"],
    "viirs-snpp": ["latitude", "longitude", "bright_ti4", "scan", "track", "acq_date", "acq_time", "satellite",
                   "instrument", "confidence", "version", "bright_ti5", "frp", "daynight", "type"],
}


def map_key() -> str:
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("FIRMS_MAP_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("No FIRMS_MAP_KEY. Create fire-calendar/.env with FIRMS_MAP_KEY=<your key> "
                 "(free: https://firms.modaps.eosdis.nasa.gov/api/map_key/)")
    return key


def get(url: str, retries: int = 4) -> str:
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, context=CTX, timeout=120) as r:
                text = r.read().decode("utf-8")
            if text.startswith("Invalid") or "Exceeding allowed transaction" in text:
                raise RuntimeError(text.strip()[:200])
            return text
        except RuntimeError:
            raise
        except Exception as e:
            err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f"request failed after {retries} tries: {err!r}")


def availability(key: str) -> dict[str, tuple[dt.date, dt.date]]:
    df = pd.read_csv(io.StringIO(get(f"{API}/data_availability/csv/{key}/all")))
    return {r.data_id: (pd.Timestamp(r.min_date).date(), pd.Timestamp(r.max_date).date())
            for r in df.itertuples()}


def fetch_range(key: str, source: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    frames, day = [], start
    while day <= end:
        n = min(WINDOW, (end - day).days + 1)
        text = get(f"{API}/area/csv/{key}/{source}/{BBOX}/{n}/{day}")
        if text.strip() and "latitude" in text.splitlines()[0]:
            frames.append(pd.read_csv(io.StringIO(text), dtype={"acq_time": str}))
        print(f"  {source} {day} +{n}d", flush=True)
        day += dt.timedelta(days=n)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def archive_lookups(before_year: int) -> tuple[pd.Series, set]:
    """Country per 0.1 deg cell and static-source locations (0.01 deg) from the archive."""
    con = duckdb.connect()
    files = [(RAW / s).as_posix() + "/*.csv*" for s in SENSORS]
    src = " UNION ALL ".join(
        f"SELECT latitude, longitude, type, regexp_extract(filename, '_(\\d{{4}})_(.+?)\\.csv', 2) AS country, "
        f"regexp_extract(filename, '_(\\d{{4}})_', 1)::INT AS year "
        f"FROM read_csv('{f}', filename=true, union_by_name=true)" for f in files)
    cc = con.execute(f"""
        SELECT round(latitude*10)::INT AS gy, round(longitude*10)::INT AS gx, mode(country) AS country
        FROM ({src}) WHERE year < {before_year} GROUP BY ALL
    """).df()
    static = con.execute(f"""
        SELECT round(latitude*100)::INT AS sy, round(longitude*100)::INT AS sx, count(*) AS n
        FROM ({src}) WHERE type IN (1, 2) AND year < {before_year} GROUP BY ALL
    """).fetchall()
    return cc.set_index(["gy", "gx"]).country, {(y, x): n for y, x, n in static}


def assign_country(df: pd.DataFrame, cc: pd.Series) -> pd.Series:
    gy = np.round(df.latitude * 10).astype(int)
    gx = np.round(df.longitude * 10).astype(int)
    out = pd.Series(pd.MultiIndex.from_arrays([gy, gx]).map(cc), index=df.index)
    miss = out.isna()
    for i in df.index[miss]:          # few points: look at the 3x3 neighbourhood
        near = [cc.get((gy[i] + a, gx[i] + b)) for a in (-1, 0, 1) for b in (-1, 0, 1)]
        near = [c for c in near if isinstance(c, str)]
        if near:
            out[i] = max(set(near), key=near.count)
    return out


# Static-mask settings per sensor, tuned by back-testing on the 2025 archive
# (lookups from 2000-2024, flags compared with FIRMS `type`):
#   VIIRS: exact 0.01 deg cell, >= 10 past static detections -> catches 95% of static, 6.5% false
#   MODIS: 3x3 neighbourhood (coarser 1 km pixels), >= 1      -> catches 62% of static, 3.7% false
MASK = {"viirs-snpp": {"min_n": 10, "nb": 0}, "modis": {"min_n": 1, "nb": 1}}


def static_flag(df: pd.DataFrame, static: dict, min_n: int = 1, nb: int = 1) -> np.ndarray:
    locs = {k for k, n in static.items() if n >= min_n}
    sy = np.round(df.latitude * 100).astype(int).to_numpy()
    sx = np.round(df.longitude * 100).astype(int).to_numpy()
    rng = range(-nb, nb + 1)
    return np.array([any((y + a, x + b) in locs for a in rng for b in rng) for y, x in zip(sy, sx)], bool)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=dt.date.today().year)
    p.add_argument("--sensors", nargs="+", default=list(SENSORS), choices=list(SENSORS))
    a = p.parse_args()
    key = map_key()
    avail = availability(key)
    print("loading archive lookups ...", flush=True)
    cc, static = archive_lookups(a.year)
    year_start, today = dt.date(a.year, 1, 1), dt.date.today()

    for sensor, (sp, nrt) in SENSORS.items():
        if sensor not in a.sensors:
            continue
        parts = []
        sp_end = min(avail.get(sp, (None, year_start - dt.timedelta(days=1)))[1], today)
        if sp in avail and sp_end >= year_start:
            parts.append(fetch_range(key, sp, max(year_start, avail[sp][0]), sp_end).assign(_src="SP"))
        nrt_start = max(year_start, sp_end + dt.timedelta(days=1)) if sp in avail else year_start
        if nrt in avail and nrt_start <= avail[nrt][1]:
            parts.append(fetch_range(key, nrt, max(nrt_start, avail[nrt][0]), avail[nrt][1]).assign(_src="NRT"))
        df = pd.concat([x for x in parts if len(x)], ignore_index=True) if parts else pd.DataFrame()
        if df.empty:
            print(f"{sensor}: no data", flush=True)
            continue
        df = clean(df, sensor, cc, static)
        if sensor == "viirs-snpp":
            df["satellite"] = "N"
            df = gap_fill_noaa20(df, key, avail, cc, static)
        for country, g in df.groupby("country"):
            dest = RAW / sensor / f"{sensor}_{a.year}_{country}.csv"
            dest.parent.mkdir(parents=True, exist_ok=True)
            g.sort_values(["acq_date", "acq_time"])[COLUMNS[sensor] + EXTRA.get(sensor, [])].to_csv(dest, index=False)
        src = df._src.value_counts().to_dict()
        print(f"{sensor}: {len(df):,} hotspots in {domain.DOMAIN} {src}, "
              f"{(df.type != 0).sum():,} flagged static, last date {df.acq_date.max()}", flush=True)
    return 0


EXTRA = {"viirs-snpp": ["sat_scale"]}   # NOAA-20 gap-fill points carry their S-NPP scaling


def clean(df: pd.DataFrame, sensor: str, cc: pd.Series, static: dict) -> pd.DataFrame:
    """Dedupe, assign country, drop non-Central-Asia, set `type` (static mask if missing)."""
    df = df.drop_duplicates(["latitude", "longitude", "acq_date", "acq_time"])
    df = df.assign(country=assign_country(df, cc))
    df = df[df.country.isin(list(domain.FIRMS_NAMES.values()))].copy()
    if "type" not in df or df["type"].isna().any():
        flag = static_flag(df, static, **MASK[sensor])
        t = pd.to_numeric(df["type"], errors="coerce") if "type" in df else pd.Series(np.nan, index=df.index)
        df["type"] = np.where(t.notna(), t, np.where(flag, 2, 0)).astype(int)
    df["acq_time"] = df.acq_time.astype(str).str.zfill(4)
    for c in COLUMNS[sensor] + EXTRA.get(sensor, []):
        if c not in df:
            df[c] = np.nan
    return df


def gap_fill_noaa20(df: pd.DataFrame, key: str, avail: dict, cc, static, overlap_days: int = 60) -> pd.DataFrame:
    """If S-NPP NRT ends before NOAA-20 NRT, fill the gap with NOAA-20 VIIRS scaled to S-NPP.

    Scale = S-NPP / NOAA-20 vegetation-fire count over the last `overlap_days` both flew.
    """
    n20 = "VIIRS_NOAA20_NRT"
    snpp_last = pd.to_datetime(df.acq_date).max().date()
    if n20 not in avail or avail[n20][1] <= snpp_last:
        return df
    ov_start = max(avail[n20][0], snpp_last - dt.timedelta(days=overlap_days - 1))
    g = fetch_range(key, n20, ov_start, avail[n20][1])
    if g.empty:
        return df
    g = clean(g, "viirs-snpp", cc, static)
    gd, sd = pd.to_datetime(g.acq_date).dt.date, pd.to_datetime(df.acq_date).dt.date
    n_s = ((sd >= ov_start) & (sd <= snpp_last) & (df.type == 0)).sum()
    n_n = ((gd >= ov_start) & (gd <= snpp_last) & (g.type == 0)).sum()
    scale = float(n_s / n_n) if n_n else 1.0
    gap = g[gd > snpp_last].assign(satellite="N20", sat_scale=round(scale, 4), _src="NRT-N20")
    print(f"  S-NPP ends {snpp_last}; NOAA-20 fills to {avail[n20][1]} "
          f"(overlap {ov_start}..{snpp_last}: S-NPP {n_s:,} vs NOAA-20 {n_n:,} -> scale {scale:.3f})", flush=True)
    return pd.concat([df, gap], ignore_index=True)


if __name__ == "__main__":
    sys.exit(main())
