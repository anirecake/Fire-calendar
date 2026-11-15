"""Daily weather per region from NASA POWER (no key): 2001 -> yesterday.

Point per region = mean position of the region's VIIRS fire detections (where fires actually
happen); regions without fires use the polygon's representative point.
Parameters: T2M_MAX (C), RH2M (%), WS10M (m/s), PRECTOTCORR (mm/day), GWETTOP (0-1 topsoil wetness).

Output: data/raw/weather/<region>.json (cache) and data/processed/weather.parquet
Usage:  python scripts/build_weather.py
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

import duckdb
import pandas as pd
import shapely
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_firms import CTX, ROOT  # noqa: E402

import domain  # noqa: E402
RAW = domain.RAW / "weather"
OUT = domain.DATA / "weather.parquet"
PARAMS = ["T2M_MAX", "RH2M", "WS10M", "PRECTOTCORR", "GWETTOP", "GWETROOT", "WS10M_MAX", "T2MDEW", "ALLSKY_SFC_SW_DWN"]
API = "https://power.larc.nasa.gov/api/temporal/daily/point"


def points() -> pd.DataFrame:
    fc = json.loads((domain.WEB_OUT / "regions.geojson").read_text(encoding="utf-8"))
    rp = {f["properties"]["id"]: shapely.point_on_surface(shape(f["geometry"])) for f in fc["features"]}
    fire = duckdb.connect().execute(f"""
        SELECT region, avg(lat) lat, avg(lon) lon, count(*) n
        FROM read_parquet('{(ROOT / 'data/processed/points.parquet').as_posix()}') WHERE sensor='VIIRS' GROUP BY 1
    """).df().set_index("region")
    rows = []
    for rid, p in rp.items():
        if rid in fire.index and fire.at[rid, "n"] >= 50:
            # use the fire centre only if it falls inside the region (concave regions)
            c = shapely.Point(fire.at[rid, "lon"], fire.at[rid, "lat"])
            poly = shape(next(f["geometry"] for f in fc["features"] if f["properties"]["id"] == rid))
            p = c if poly.contains(c) else p
        rows.append({"region": rid, "lat": round(p.y, 3), "lon": round(p.x, 3)})
    return pd.DataFrame(rows)


def fetch(row, end: str, retries: int = 4) -> str:
    dest = RAW / f"{row.region}.json"
    if dest.exists():
        p = json.loads(dest.read_text())["properties"]["parameter"]
        if set(PARAMS) <= set(p) and max(p["T2M_MAX"]) >= end:
            return f"cached {row.region}"
    url = (f"{API}?parameters={','.join(PARAMS)}&community=AG&longitude={row.lon}&latitude={row.lat}"
           f"&start=20010101&end={end}&format=JSON")
    for a in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, context=CTX, timeout=180) as r:
                data = r.read()
            json.loads(data)
            dest.write_bytes(data)
            return f"ok {row.region}"
        except Exception as e:
            err = e
            time.sleep(5 * a)
    return f"FAIL {row.region}: {err!r}"


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    pts = points()
    end = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")
    with cf.ThreadPoolExecutor(4) as ex:
        for msg in ex.map(lambda r: fetch(r, end), pts.itertuples()):
            print(msg, flush=True)
    frames = []
    for r in pts.itertuples():
        f = RAW / f"{r.region}.json"
        if not f.exists():
            continue
        p = json.loads(f.read_text())["properties"]["parameter"]
        d = pd.DataFrame(p)
        d.index = pd.to_datetime(d.index, format="%Y%m%d")
        d = d.where(d > -900)                       # POWER fill value -999
        frames.append(d.assign(region=r.region, wlat=r.lat, wlon=r.lon))
    w = pd.concat(frames).rename_axis("date").reset_index()
    w.columns = [c.lower() for c in w.columns]
    w.to_parquet(OUT, index=False, compression="zstd")
    print(f"wrote {OUT}: {len(w):,} rows, {w.region.nunique()} regions, {w.date.min().date()}..{w.date.max().date()}")
    print("missing share:", w[[c.lower() for c in PARAMS]].isna().mean().round(3).to_dict())


if __name__ == "__main__":
    main()
