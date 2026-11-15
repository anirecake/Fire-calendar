"""After-fire reports: one fact sheet per large fire, for journalists and emergency services.

For the largest fires (recent ones first, plus the biggest on record) this script collects:
  footprint     the fire's VIIRS 375 m detections (same linking rule as build_events.py), day-by-day growth,
                active-fire footprint area (unique ~400 m positions x 0.14 km2, NOT the burned area)
  land cover    what was burning: ESA WorldCover 2021 (10 m, read at ~37 m) inside the footprint
  around        settlements within 15 km of the footprint, main roads and power lines crossing it (OpenStreetMap)
  weather       NASA POWER for the province: heat, dryness, wind and rain during the fire vs the usual
  history       how big this fire is compared with every fire in the province since 2012
  burned area   NASA MODIS MCD64A1 (500 m) pixels burned during the fire next to its detections; over all fires a
                calibration: km2 burned per km2 of VIIRS footprint (one unit for every fire: burned km2)
  radar         (optional) NISAR L-band before/after comparison made with SARWatch: change in radar
                intensity inside the footprint vs the rest of the square, and the area where the
                signal dropped clearly more than the background (an experimental burned-area estimate)
Output: data/processed/fires/index.json + fires/<id>.json (+ radar images and SARWatch PDF reports in fires/<id>/)

Usage: python scripts/build_fire_reports.py [--nisar DIR] [--recent 25] [--alltime 10], then --modis-only
       FC_DOMAIN=us python scripts/build_fire_reports.py --nisar DIR
DIR holds SARWatch results, one folder per fire with job.json (event_id, domain, layers) and before/after/delta PNG.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_events as be  # noqa: E402  (same linking rule)
import domain  # noqa: E402

DATA = domain.DATA
OUT = DATA / "fires"
WC_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
WC_K = 3000                      # WorldCover overview 1: 3000 px per degree (~37 m)
WC_CLASSES = {10: "tree", 20: "shrub", 30: "grass", 40: "crop", 50: "built", 60: "bare", 70: "snow",
              80: "water", 90: "wetland", 95: "mangrove", 100: "moss"}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
UA = {"User-Agent": "FireCalendar/1.0 (NASA Space Apps research prototype)"}
PLACE_KM = 15.0
RADAR_T_DB = 2.0                 # "clear drop" in radar intensity, dB


def log(*a):
    print(*a, flush=True)


def km_lat(d):
    return d / 111.32


def km_lon(d, lat):
    return d / (111.32 * math.cos(math.radians(lat)))


# --------------------------------------------------------------------------- fire footprint
def fire_points(con, ev) -> pd.DataFrame:
    """VIIRS detections of this fire: re-link candidate points around it and keep the matching component."""
    half = ev.extent_km * 0.75 + 5
    s, e = pd.Timestamp(ev.start) - pd.Timedelta(days=3), pd.Timestamp(ev.end) + pd.Timedelta(days=3)
    pts = con.execute(f"""
        SELECT lat, lon, date, time, frp, daynight FROM read_parquet('{(DATA / 'points.parquet').as_posix()}')
        WHERE sensor = 'VIIRS' AND date BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
        [s.date(), e.date(), ev.lat - km_lat(half), ev.lat + km_lat(half),
         ev.lon - km_lon(half, ev.lat), ev.lon + km_lon(half, ev.lat)]).df()
    pts["date"] = pd.to_datetime(pts.date)
    pts["day"] = (pts.date - pd.Timestamp("2012-01-01")).dt.days.astype(np.int32)
    pts["px"] = np.round(pts.lon / be.CELL).astype(np.int32)
    pts["py"] = np.round(pts.lat / be.CELL).astype(np.int32)
    nodes = pts[["px", "py", "day"]].drop_duplicates().reset_index(drop=True)
    nodes["comp"] = be.link(nodes)
    pts = pts.merge(nodes, on=["px", "py", "day"])
    inside = pts[(pts.date >= pd.Timestamp(ev.start)) & (pts.date <= pd.Timestamp(ev.end))]
    comp = inside.comp.value_counts().idxmax()
    out = pts[pts.comp == comp].copy()
    out["fx"] = np.round(out.lon / be.PIX).astype(np.int32)
    out["fy"] = np.round(out.lat / be.PIX).astype(np.int32)
    return out.sort_values(["date", "time"]).reset_index(drop=True)


def growth(p: pd.DataFrame) -> list[dict]:
    seen, rows, total = set(), [], 0
    for d, g in p.groupby("date"):
        cells = set(zip(g.fx, g.fy))
        new = len(cells - seen)
        seen |= cells
        total += new
        rows.append({"date": d.date().isoformat(), "detections": int(len(g)), "new_km2": round(new * be.PIX_KM2, 2),
                     "total_km2": round(total * be.PIX_KM2, 2), "frp_max_mw": round(float(g.frp.max()), 1)})
    return rows


# --------------------------------------------------------------------------- land cover (ESA WorldCover)
def worldcover(w, s, e, n):
    """Classes on a 1/3000 deg grid covering [w, e] x [s, n]; returns (array north-up, W, N) in grid units."""
    import rasterio
    W, E, S, N = math.floor(w * WC_K), math.ceil(e * WC_K), math.floor(s * WC_K), math.ceil(n * WC_K)
    arr = np.zeros((N - S, E - W), np.uint8)
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_MAX_RETRY="3")
    with rasterio.Env(**env):
        for ty in range(math.floor(s / 3) * 3, math.ceil(n), 3):
            for tx in range(math.floor(w / 3) * 3, math.ceil(e), 3):
                tile = f"{'N' if ty >= 0 else 'S'}{abs(ty):02d}{'E' if tx >= 0 else 'W'}{abs(tx):03d}"
                y0, y1 = max(S, ty * WC_K), min(N, (ty + 3) * WC_K)
                x0, x1 = max(W, tx * WC_K), min(E, (tx + 3) * WC_K)
                if y0 >= y1 or x0 >= x1:
                    continue
                try:
                    with rasterio.open("/vsicurl/" + WC_URL.format(tile=tile), overview_level=1) as src:   # 1 = 4x coarser
                        top = (ty + 3) * WC_K
                        win = ((top - y1, top - y0), (x0 - tx * WC_K, x1 - tx * WC_K))
                        arr[N - y1:N - y0, x0 - W:x1 - W] = src.read(1, window=win)
                except Exception as ex:                       # ocean tile or network: leave zeros (unknown)
                    log(f"    worldcover {tile}: {str(ex)[:80]}")
    return arr, W, N


def land_cover(p: pd.DataFrame) -> dict:
    cells = p[["fx", "fy"]].drop_duplicates()
    h = be.PIX / 2
    w, e = cells.fx.min() * be.PIX - h, cells.fx.max() * be.PIX + h
    s, n = cells.fy.min() * be.PIX - h, cells.fy.max() * be.PIX + h
    arr, W, N = worldcover(w, s, e, n)
    counts = np.zeros(256, np.int64)
    for fx, fy in zip(cells.fx, cells.fy):
        c0, c1 = int(math.floor((fx * be.PIX - h) * WC_K)) - W, int(math.ceil((fx * be.PIX + h) * WC_K)) - W
        r0, r1 = N - int(math.ceil((fy * be.PIX + h) * WC_K)), N - int(math.floor((fy * be.PIX - h) * WC_K))
        counts += np.bincount(arr[max(r0, 0):r1, max(c0, 0):c1].ravel(), minlength=256)
    known = counts[list(WC_CLASSES)].sum()
    area = len(cells) * be.PIX_KM2
    if not known:
        return {"available": False}
    shares = {WC_CLASSES[k]: counts[k] / known for k in WC_CLASSES if counts[k]}
    return {"available": True, "source": "ESA WorldCover 2021 (10 m)",
            "classes": [{"class": c, "share": round(v, 3), "km2": round(v * area, 2)}
                        for c, v in sorted(shares.items(), key=lambda kv: -kv[1]) if v >= 0.005]}


# --------------------------------------------------------------------------- around (OpenStreetMap)
def overpass(q: str, cache: Path | None = None) -> dict | None:
    """One Overpass query (main server; the public mirrors were unreliable). Cached on disk; None if it failed."""
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    for attempt in range(2):
        try:
            r = urllib.request.Request(OVERPASS[0], data=urllib.parse.urlencode({"data": q}).encode(), headers=UA)
            with urllib.request.urlopen(r, timeout=90) as resp:
                d = json.loads(resp.read())
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(d), encoding="utf-8")
            return d
        except Exception as ex:
            log(f"    overpass retry {attempt + 1}: {str(ex)[:80]}")
            time.sleep(5)
    return None


def around(p: pd.DataFrame, ev) -> dict:
    cells = p[["fx", "fy"]].drop_duplicates()
    cl = np.c_[cells.fy.to_numpy() * be.PIX, cells.fx.to_numpy() * be.PIX]
    pad = PLACE_KM + 2
    s, n = cl[:, 0].min() - km_lat(pad), cl[:, 0].max() + km_lat(pad)
    w, e = cl[:, 1].min() - km_lon(pad, ev.lat), cl[:, 1].max() + km_lon(pad, ev.lat)
    bb = f"{s:.4f},{w:.4f},{n:.4f},{e:.4f}"
    fs, fn = cl[:, 0].min() - 0.01, cl[:, 0].max() + 0.01
    fw, fe = cl[:, 1].min() - 0.01, cl[:, 1].max() + 0.01
    fb = f"{fs:.4f},{fw:.4f},{fn:.4f},{fe:.4f}"
    cache = domain.RAW / "osm_cache"
    dp = overpass(f"""[out:json][timeout:180];(node["place"~"^(city|town|village|hamlet)$"]({bb}););out body;""",
                  cache / f"{ev.id}_places.json")
    di = overpass(f"""[out:json][timeout:180];(way["highway"~"^(motorway|trunk|primary|secondary)$"]({fb});
        way["power"="line"]({fb});way["railway"="rail"]({fb}););out tags geom;""", cache / f"{ev.id}_infra.json")
    d = {"elements": (dp or {}).get("elements", []) + (di or {}).get("elements", [])}

    def dist_km(lat, lon):
        dy = (cl[:, 0] - lat) * 111.32
        dx = (cl[:, 1] - lon) * 111.32 * math.cos(math.radians(lat))
        return float(np.sqrt(dx * dx + dy * dy).min())

    places, roads, power, rail = [], {}, 0, 0
    for el in d.get("elements", []):
        tg = el.get("tags", {})
        if el["type"] == "node":
            km = dist_km(el["lat"], el["lon"])
            if km > PLACE_KM:
                continue
            pop = tg.get("population", "").replace(" ", "").replace(",", "")
            places.append({"name": tg.get("name"), "ru": tg.get("name:ru"), "kk": tg.get("name:kk"), "en": tg.get("name:en"),
                           "kind": tg.get("place"), "km": round(km, 1), "population": int(pop) if pop.isdigit() else None,
                           "lat": round(el["lat"], 4), "lon": round(el["lon"], 4)})
            continue
        geom = el.get("geometry") or []
        if not any(dist_km(g["lat"], g["lon"]) < 0.5 for g in geom[::2] + geom[-1:]):
            continue                                         # passes the box but not the burning area
        if "power" in tg:
            power += 1
        elif "railway" in tg:
            rail += 1
        else:
            key = tg.get("ref") or tg.get("name") or tg.get("highway")
            roads[key] = tg.get("highway")
    places.sort(key=lambda x: x["km"])
    known = [x["population"] for x in places if x["population"]]
    return {"source": "OpenStreetMap", "radius_km": PLACE_KM, "places_ok": dp is not None, "infra_ok": di is not None,
            "places": places[:20], "n_places": len(places),
            "inside_or_1km": sum(1 for x in places if x["km"] <= 1),
            "population_known": int(sum(known)) if known else None,
            "population_known_places": len(known),
            "roads": [{"ref": k, "kind": v} for k, v in list(roads.items())[:12]], "n_roads": len(roads),
            "power_lines": power, "railways": rail}


# --------------------------------------------------------------------------- weather + history
def weather(wx: pd.DataFrame, ev) -> dict | None:
    g = wx[wx.region == ev.region]
    if g.empty:
        return None
    s, e = pd.Timestamp(ev.start), pd.Timestamp(ev.end)
    during = g[(g.date >= s) & (g.date <= e)]
    if during.empty:
        return None
    doy0, doy1 = s.day_of_year, e.day_of_year
    clim = g[(g.date.dt.year < s.year) & (g.date.dt.day_of_year.between(doy0, max(doy1, doy0)))]
    before = g[(g.date < s) & (g.date >= s - pd.Timedelta(days=30))]
    pre_norm = []
    for y in sorted(set(g.date.dt.year) - {s.year}):
        ys = s.replace(year=y) if not (s.month == 2 and s.day == 29) else s.replace(year=y, day=28)
        pre_norm.append(g[(g.date < ys) & (g.date >= ys - pd.Timedelta(days=30))].prectotcorr.sum())
    r = lambda v, k=1: None if v is None or pd.isna(v) else round(float(v), k)
    return {"source": "NASA POWER (province average)",
            "tmax": r(during.t2m_max.max()), "tmax_usual": r(clim.t2m_max.mean()),
            "rh_min": r(during.rh2m.min()), "rh_usual": r(clim.rh2m.mean()),
            "wind_max": r(during.ws10m_max.max()), "wind_usual": r(clim.ws10m_max.mean()),
            "rain_during_mm": r(during.prectotcorr.sum()),
            "rain_30d_before_mm": r(before.prectotcorr.sum()),
            "rain_30d_before_usual_mm": r(np.mean(pre_norm)) if pre_norm else None}


def history(evs: pd.DataFrame, ev) -> dict:
    reg = evs[evs.region == ev.region]
    big = reg[reg.type == "large"]
    yr = big[big.start.dt.year == pd.Timestamp(ev.start).year]
    return {"since": str(evs.start.min().date()), "rank_region": int((reg.area_km2 > ev.area_km2).sum() + 1),
            "fires_region": int(len(reg)), "rank_all": int((evs.area_km2 > ev.area_km2).sum() + 1),
            "fires_all": int(len(evs)), "large_region_same_year": int(len(yr)),
            "large_region_per_year": round(len(big) / max(1, big.start.dt.year.nunique()), 1),
            "share_recurrent_nearby": None if pd.isna(ev.recurrence) else round(float(ev.recurrence), 2)}


# --------------------------------------------------------------------------- NISAR radar (SARWatch output)
def radar(folder: Path, p: pd.DataFrame, ev, dest: Path, con) -> dict | None:
    from PIL import Image
    meta = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    res = meta["job"]["result"]
    layer = {l["id"]: l for l in res["layers"]}
    if "delta" not in layer or not (folder / "delta.png").exists():
        return None
    w, s, e, n = layer["delta"]["bounds"]
    lim = float(layer["delta"]["display_range_db"][1])
    img = np.asarray(Image.open(folder / "delta.png").convert("RGBA")).astype(np.float32) / 255
    R, G, B, A = img[..., 0], img[..., 1], img[..., 2], img[..., 3]
    sgn = np.where(B < R, 1 - B, -(1 - R))                   # SARWatch colour ramp: red = increase, blue = decrease
    delta = sgn * lim
    valid = A > 0.5
    h, wd = delta.shape
    merc = lambda lat: math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    inv = lambda y: math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)
    ys = np.linspace(merc(n), merc(s), h + 1)
    lat_c = np.array([inv((ys[i] + ys[i + 1]) / 2) for i in range(h)])
    lon_c = w + (np.arange(wd) + 0.5) * (e - w) / wd
    px_km2 = ((e - w) / wd * 111.32 * np.cos(np.radians(lat_c)))[:, None] * \
             (np.abs(np.diff([inv(y) for y in ys])) * 111.32)[:, None]
    px_km2 = np.broadcast_to(px_km2, delta.shape)
    FX = np.round(lon_c / be.PIX).astype(np.int64)[None, :].repeat(h, 0)
    FY = np.round(lat_c / be.PIX).astype(np.int64)[:, None].repeat(wd, 1)
    cells = set(zip(p.fx.tolist(), p.fy.tolist()))
    near = {(x + dx, y + dy) for x, y in cells for dx in range(-5, 6) for dy in range(-5, 6)}  # ~2 km ring
    key = lambda X, Y: X * 1_000_003 + Y
    kin = np.isin(key(FX, FY), np.fromiter((key(x, y) for x, y in cells), np.int64))
    knear = np.isin(key(FX, FY), np.fromiter((key(x, y) for x, y in near), np.int64))
    # background = pixels with NO fire detection at all between the two radar passes (other fires burn scars too)
    src = res.get("sources") or [meta.get("before", {}), meta.get("after", {})]
    b0, a0 = src[0].get("start", "")[:10], src[1].get("start", "")[:10]
    other = con.execute(f"""SELECT DISTINCT round(lon / {be.PIX})::BIGINT, round(lat / {be.PIX})::BIGINT
        FROM read_parquet('{(DATA / 'points.parquet').as_posix()}') WHERE sensor = 'VIIRS' AND date BETWEEN ? AND ?
        AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?""", [b0, a0, w, e, s, n]).fetchall()
    burnt = {(x + dx, y + dy) for x, y in other for dx in range(-3, 4) for dy in range(-3, 4)}
    kother = np.isin(key(FX, FY), np.fromiter((key(x, y) for x, y in burnt), np.int64)) if burnt else np.zeros_like(kin)
    inside, outside = valid & kin, valid & ~knear & ~kother
    if inside.sum() < 30 or outside.sum() < 300:
        return None
    t = min(RADAR_T_DB, 0.9 * lim)
    drop_in, drop_out = float((delta[inside] <= -t).mean()), float((delta[outside] <= -t).mean())
    fp_km2 = len(cells) * be.PIX_KM2
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("before.png", "after.png", "delta.png", "report-en.pdf", "report-ru.pdf", "report-kk.pdf"):
        if (folder / f).exists():
            shutil.copy2(folder / f, dest / f)
    obs = res.get("observations") or [{}]
    contrast = float(np.median(delta[inside]) - np.median(delta[outside]))
    return {"source": "NASA NISAR L-band GCOV via SARWatch", "status": "experimental, not independently validated",
            "before": src[0].get("start", "")[:10], "after": src[1].get("start", "")[:10],
            "maturity": src[0].get("maturity") or src[0].get("processing_level"),
            "channel": obs[0].get("channel"), "grid_m": (obs[0].get("pixel_spacing_m") or [None])[0],
            "bounds": [w, s, e, n], "range_db": lim, "threshold_db": round(t, 2),
            "median_inside_db": round(float(np.median(delta[inside])), 2),
            "median_outside_db": round(float(np.median(delta[outside])), 2),
            "contrast_db": round(contrast, 2),
            "drop_share_inside": round(drop_in, 3), "drop_share_outside": round(drop_out, 3),
            "burned_estimate_km2": round(max(0.0, drop_in - drop_out) * fp_km2, 1),
            "drop_area_zone_km2": round(float(px_km2[valid & knear & (delta <= -t)].sum()), 1),
            "signal": "clear" if contrast <= -1.0 and drop_in >= 2 * max(drop_out, 0.02) else
                      "weak" if contrast <= -0.3 else "none",
            "images": ["before.png", "after.png", "delta.png"],
            "reports": [f for f in ("report-en.pdf", "report-ru.pdf", "report-kk.pdf") if (folder / f).exists()]}


# --------------------------------------------------------------------------- MODIS burned area (MCD64A1)
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/modis-64A1-061"
_tok: dict = {}


def pc_token() -> str:
    if not _tok or time.time() > _tok["t"] + 1800:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(PC_TOKEN, headers=UA), timeout=60).read())
        _tok.update(token=d["token"], t=time.time())
    return _tok["token"]


_sas: dict = {}


def pc_sign(href: str) -> str:
    """Signed URL for a Planetary Computer asset: sign once per storage container, reuse the SAS query."""
    base = "/".join(href.split("/")[:4])                 # https://<account>.blob.core.windows.net/<container>
    if base not in _sas or time.time() > _sas[base][1] + 1800:
        u = "https://planetarycomputer.microsoft.com/api/sas/v1/sign?href=" + urllib.parse.quote(href, safe="")
        for attempt in range(5):
            try:
                signed = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=60).read())["href"]
                break
            except Exception:
                time.sleep(10 * (attempt + 1))
        else:
            raise RuntimeError("could not sign Planetary Computer URL")
        _sas[base] = (signed.split("?", 1)[1], time.time())
    return href + "?" + _sas[base][0]


def modis_burned(p: pd.DataFrame, ev) -> dict | None:
    """NASA MODIS MCD64A1 (500 m, monthly burn date) pixels that burned during this fire within ~1.3 km of its
    VIIRS detections: an independent burned-area estimate for the same fire."""
    import rasterio
    from rasterio.warp import transform, transform_bounds
    cells = set(zip(p.fx.tolist(), p.fy.tolist()))
    near = {(x + dx, y + dy) for x, y in cells for dx in range(-3, 4) for dy in range(-3, 4)}
    w, s, e, n = p.lon.min() - 0.02, p.lat.min() - 0.02, p.lon.max() + 0.02, p.lat.max() + 0.02
    s0, e0 = pd.Timestamp(ev.start) - pd.Timedelta(days=3), pd.Timestamp(ev.end) + pd.Timedelta(days=10)
    body = {"collections": ["modis-64A1-061"], "bbox": [w, s, e, n],
            "datetime": f"{s0:%Y-%m}-01/{e0:%Y-%m-%d}", "limit": 50}
    try:
        r = urllib.request.Request(PC_STAC, data=json.dumps(body).encode(), headers={**UA, "Content-Type": "application/json"})
        items = json.loads(urllib.request.urlopen(r, timeout=90).read())["features"]
    except Exception as ex:
        log(f"    mcd64a1 search: {str(ex)[:80]}")
        return None
    need = [str(m) for m in pd.period_range(s0, e0, freq="M")]
    got = sorted({it["properties"]["start_datetime"][:7] for it in items})
    if not items:
        return {"available": False, "missing_months": need}
    burned, px_km2, failed = set(), None, set()
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="3")
    with rasterio.Env(**env):
        for it in items:
            y = int(it["properties"]["start_datetime"][:4])
            try:
                with rasterio.open("/vsicurl/" + pc_sign(it["assets"]["Burn_Date"]["href"])) as src:
                    l, b, rr, t = transform_bounds("EPSG:4326", src.crs, w, s, e, n, densify_pts=21)
                    r0, c0 = src.index(l, t)
                    r1, c1 = src.index(rr, b)
                    r0, r1 = max(0, min(r0, r1)), min(src.height, max(r0, r1) + 1)
                    c0, c1 = max(0, min(c0, c1)), min(src.width, max(c0, c1) + 1)
                    if r0 >= r1 or c0 >= c1:
                        continue
                    arr = src.read(1, window=((r0, r1), (c0, c1)))
                    px_km2 = abs(src.transform.a * src.transform.e) / 1e6
                    rows, cols = np.nonzero(arr > 0)
                    if not len(rows):
                        continue
                    dates = pd.Timestamp(f"{y}-01-01") + pd.to_timedelta(arr[rows, cols].astype(int) - 1, unit="D")
                    ok = np.asarray((dates >= s0) & (dates <= e0))
                    rows, cols = rows[ok] + r0, cols[ok] + c0
                    if not len(rows):
                        continue
                    xs = src.transform.c + (cols + 0.5) * src.transform.a
                    ys = src.transform.f + (rows + 0.5) * src.transform.e
                    lon, lat = transform(src.crs, "EPSG:4326", xs.tolist(), ys.tolist())
                    fx = np.round(np.asarray(lon) / be.PIX).astype(int)
                    fy = np.round(np.asarray(lat) / be.PIX).astype(int)
                    for a_, b_, x_, y_ in zip(fx, fy, xs, ys):
                        if (a_, b_) in near:
                            burned.add((round(x_), round(y_)))
            except Exception as ex:
                failed.add(it["properties"]["start_datetime"][:7])
                log(f"    mcd64a1 {it['id']}: {str(ex)[:80]}")
    return {"available": True, "source": "NASA MODIS MCD64A1 v6.1 burned area (500 m), via Microsoft Planetary Computer",
            "burned_km2": round(len(burned) * (px_km2 or 0.2147), 1), "months": got,
            "missing_months": sorted({m for m in need if m not in got} | (failed & set(need)))}


def calibration(reps: list[dict]) -> dict | None:
    """Burned area (MODIS MCD64A1) per km2 of VIIRS active-fire footprint, over all fires with complete MODIS months."""
    pairs = [(r["footprint_km2"], r["modis_burned"]["burned_km2"]) for r in reps
             if r.get("modis_burned") and r["modis_burned"].get("available") and not r["modis_burned"]["missing_months"]
             and r["footprint_km2"] > 0]
    if len(pairs) < 5:
        return None
    fp, mb = np.array(pairs).T
    ratio = mb / fp
    return {"n_fires": int(len(pairs)), "median_ratio": round(float(np.median(ratio)), 2),
            "q25": round(float(np.quantile(ratio, 0.25)), 2), "q75": round(float(np.quantile(ratio, 0.75)), 2),
            "slope_through_origin": round(float((fp * mb).sum() / (fp * fp).sum()), 2),
            "r_log": round(float(np.corrcoef(np.log1p(fp), np.log1p(mb))[0, 1]), 3),
            "meaning": "km2 burned (MODIS MCD64A1) per km2 of VIIRS 375 m active-fire footprint"}


def add_modis() -> None:
    """Add the MODIS burned-area estimate to every existing report and the calibration to index.json."""
    con = duckdb.connect()
    evs = con.execute(f"SELECT * FROM read_parquet('{(DATA / 'events.parquet').as_posix()}')").df()
    evs["start"], evs["end"] = pd.to_datetime(evs.start), pd.to_datetime(evs.end)
    ix = json.loads((OUT / "index.json").read_text(encoding="utf-8"))
    reps = []
    for f in ix["fires"]:
        rep = json.loads((OUT / f"{f['id']}.json").read_text(encoding="utf-8"))
        ev = next(evs[evs.id == f["id"]].itertuples())
        rep["modis_burned"] = modis_burned(fire_points(con, ev), ev)
        mb = rep["modis_burned"]
        f["modis_km2"] = mb["burned_km2"] if mb and mb.get("available") else None
        reps.append(rep)
        log(f"  {f['id']} footprint {rep['footprint_km2']:.0f} km2, MODIS burned {f['modis_km2']} km2, "
            f"missing {mb['missing_months'] if mb else '?'}")
    ix["calibration"] = calibration(reps)
    for f, rep in zip(ix["fires"], reps):                   # every report carries the domain calibration too
        rep["calibration"] = ix["calibration"]
        (OUT / f"{f['id']}.json").write_text(json.dumps(rep, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "index.json").write_text(json.dumps(ix, ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"calibration: {ix['calibration']}")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nisar", type=Path, help="folder with SARWatch results (one sub-folder per fire)")
    ap.add_argument("--recent", type=int, default=25, help="largest fires since --since")
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--alltime", type=int, default=10, help="largest fires on record")
    ap.add_argument("--only", type=int, nargs="*", help="rebuild only these event ids")
    ap.add_argument("--modis-only", action="store_true", help="only add the MODIS MCD64A1 burned area + calibration")
    a = ap.parse_args()
    if a.modis_only:
        add_modis()
        return
    con = duckdb.connect()
    evs = con.execute(f"SELECT * FROM read_parquet('{(DATA / 'events.parquet').as_posix()}')").df()
    evs["start"], evs["end"] = pd.to_datetime(evs.start), pd.to_datetime(evs.end)
    radar_dirs = {}
    if a.nisar and a.nisar.exists():
        for f in a.nisar.glob("*/job.json"):
            m = json.loads(f.read_text(encoding="utf-8"))
            if m.get("domain", "ca") == domain.DOMAIN:
                radar_dirs[int(m["event_id"])] = f.parent
    large = evs[evs.type == "large"]
    pick = pd.concat([large[large.start >= a.since].nlargest(a.recent, "area_km2"),
                      large.nlargest(a.alltime, "area_km2"), evs[evs.id.isin(list(radar_dirs))]]).drop_duplicates("id")
    if a.only:
        pick = evs[evs.id.isin(a.only)]
    wx = pd.read_parquet(DATA / "weather.parquet", columns=["region", "date", "t2m_max", "rh2m", "ws10m_max", "prectotcorr"])
    wx["date"] = pd.to_datetime(wx.date)
    OUT.mkdir(exist_ok=True)
    index = {}
    old = OUT / "index.json"
    if a.only and old.exists():
        index = {f["id"]: f for f in json.loads(old.read_text(encoding="utf-8"))["fires"]}
    log(f"{len(pick)} fires, {len(radar_dirs)} with radar")
    for ev in pick.itertuples():
        t0 = time.time()
        p = fire_points(con, ev)
        rep = {"id": int(ev.id), "start": ev.start.date().isoformat(), "end": ev.end.date().isoformat(),
               "duration_days": int(ev.duration_days), "detections": int(len(p)), "region": ev.region,
               "country": ev.country, "lat": float(ev.lat), "lon": float(ev.lon), "type": ev.type,
               "area_km2": float(ev.area_km2), "frp_max_mw": float(ev.frp_max_mw), "frp_sum_mw": float(ev.frp_sum_mw),
               "night_share": float(ev.night_share), "extent_km": float(ev.extent_km),
               "spread_km_day": float(ev.spread_km_day), "intense": bool(ev.intense),
               "footprint_km2": round(p[["fx", "fy"]].drop_duplicates().shape[0] * be.PIX_KM2, 2),
               "growth": growth(p)}
        cells = p.drop_duplicates(["fx", "fy"])
        rep["bbox"] = [round(float(cells.lon.min()), 4), round(float(cells.lat.min()), 4),
                       round(float(cells.lon.max()), 4), round(float(cells.lat.max()), 4)]
        q = p if len(p) <= 4000 else p.nlargest(4000, "frp")
        rep["points"] = [[round(float(r.lat), 4), round(float(r.lon), 4), int((r.date - ev.start).days), round(float(r.frp), 1),
                          1 if r.daynight == "N" else 0] for r in q.itertuples()]
        rep["land_cover"] = land_cover(p)
        rep["around"] = around(p, ev)
        rep["weather"] = weather(wx, ev)
        rep["history"] = history(evs, ev)
        rep["radar"] = radar(radar_dirs[int(ev.id)], p, ev, OUT / str(ev.id), con) if int(ev.id) in radar_dirs else None
        (OUT / f"{ev.id}.json").write_text(json.dumps(rep, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        lc = rep["land_cover"].get("classes", [{}])[0] if rep["land_cover"].get("available") else {}
        index[int(ev.id)] = {k: rep[k] for k in ("id", "start", "end", "duration_days", "region", "country", "lat", "lon",
                                                 "area_km2", "frp_max_mw", "intense", "bbox")} | {
            "radar": bool(rep["radar"]), "radar_signal": rep["radar"]["signal"] if rep["radar"] else None,
            "main_cover": lc.get("class"), "n_places": rep["around"]["n_places"],
            "nearest": rep["around"]["places"][0] if rep["around"]["places"] else None}
        log(f"  {ev.id} {rep['start']} {ev.region} {ev.area_km2:.0f} km2: {len(p)} det, cover {lc.get('class')}, "
            f"{rep['around']['n_places']} places, radar {rep['radar']['signal'] if rep['radar'] else '-'} ({time.time() - t0:.0f}s)")
        time.sleep(2.0)                                      # be polite to Overpass
    fires = sorted(index.values(), key=lambda f: f["start"], reverse=True)
    (OUT / "index.json").write_text(json.dumps({"built": dt.date.today().isoformat(), "fires": fires},
                                               ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"wrote {len(fires)} fire reports to {OUT}")


if __name__ == "__main__":
    main()
