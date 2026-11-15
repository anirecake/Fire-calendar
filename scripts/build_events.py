"""From hotspots to fire EVENTS with properties (VIIRS 375 m, 2012 -> today).

Clustering: two VIIRS detections belong to the same fire if they fall in neighbouring
~1 km cells (0.01 deg, 3x3) and are at most GAP_DAYS apart (clouds, satellite gaps).
Connected components of that graph = fire events.

Properties per event
  start, end, duration_days, detections
  area_km2        active-fire footprint: unique ~375 m positions x 0.14 km2 (not burned area)
  frp_max_mw      peak fire radiative power (intensity)
  frp_sum_mw      FRP summed over detections (proxy for energy released)
  night_share     share of night-time detections (fires that keep burning at night)
  extent_km       size of the event's bounding box (diagonal)
  spread_km_day   farthest detection from the first-day centre / duration
  recurrence      share of previous years with fire in the same ~5 km cell within +-1 month
  type            large wildfire | recurrent small burn | small fire   (rules below)

Usage: python scripts/build_events.py
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

ROOT = Path(__file__).resolve().parents[1]
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import domain  # noqa: E402
DATA = domain.DATA
GAP_DAYS = 2
CELL = 0.01            # linking cell (~1 km)
PIX = 0.004            # footprint position (~375-450 m)
PIX_KM2 = 0.14         # VIIRS I-band pixel area at nadir
REC_CELL = 0.05        # recurrence cell (~5 km)

# classification rules (transparent on purpose; thresholds shown in the UI)
LARGE_AREA_KM2 = 10.0
LARGE_DURATION_D, LARGE_EXTENT_KM = 3, 5.0
SMALL_AREA_KM2, SMALL_DURATION_D = 2.0, 2
RECURRENT_SHARE = 0.5
INTENSE_FRP_MW = 100.0


def link(nodes: pd.DataFrame) -> np.ndarray:
    """Connected components over (px, py, day) nodes."""
    n = len(nodes)
    key = pd.Series(np.arange(n), index=pd.MultiIndex.from_frame(nodes[["px", "py", "day"]]))
    rows, cols = [], []
    offsets = [(dx, dy, dt) for dt in range(0, GAP_DAYS + 1) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               if (dt > 0) or (dx, dy) > (0, 0)]           # same day: half the neighbours (undirected)
    for dx, dy, dt in offsets:
        shifted = pd.MultiIndex.from_arrays([nodes.px + dx, nodes.py + dy, nodes.day - dt])
        j = key.reindex(shifted).to_numpy()
        ok = ~np.isnan(j)
        rows.append(np.arange(n)[ok]); cols.append(j[ok].astype(np.int64))
    r, c = np.concatenate(rows), np.concatenate(cols)
    g = coo_matrix((np.ones(len(r), np.int8), (r, c)), shape=(n, n))
    return connected_components(g, directed=False)[1]


def haversine_km(lat1, lon1, lat2, lon2):
    p = np.pi / 180
    a = np.sin((lat2 - lat1) * p / 2) ** 2 + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * np.arcsin(np.sqrt(a))


def main() -> None:
    con = duckdb.connect()
    pts = con.execute(f"""
        SELECT lat, lon, date, time, frp, daynight, region, country
        FROM read_parquet('{(DATA / 'points.parquet').as_posix()}') WHERE sensor = 'VIIRS'
    """).df()
    pts["date"] = pd.to_datetime(pts.date)
    pts["day"] = (pts.date - pd.Timestamp("2012-01-01")).dt.days.astype(np.int32)
    pts["px"] = np.round(pts.lon / CELL).astype(np.int32)
    pts["py"] = np.round(pts.lat / CELL).astype(np.int32)
    print(f"{len(pts):,} VIIRS detections", flush=True)

    nodes = pts[["px", "py", "day"]].drop_duplicates().reset_index(drop=True)
    print(f"{len(nodes):,} cell-days, linking ...", flush=True)
    nodes["event"] = link(nodes)
    pts = pts.merge(nodes, on=["px", "py", "day"], how="left")

    print("event properties ...", flush=True)
    pts["fx"] = np.round(pts.lon / PIX).astype(np.int32)
    pts["fy"] = np.round(pts.lat / PIX).astype(np.int32)
    g = pts.groupby("event")
    ev = g.agg(start=("date", "min"), end=("date", "max"), detections=("lat", "size"),
               frp_max_mw=("frp", "max"), frp_sum_mw=("frp", "sum"),
               night_share=("daynight", lambda s: float((s == "N").mean())),
               lat=("lat", "mean"), lon=("lon", "mean"),
               lat_min=("lat", "min"), lat_max=("lat", "max"), lon_min=("lon", "min"), lon_max=("lon", "max"),
               region=("region", lambda s: s.mode().iat[0]), country=("country", lambda s: s.mode().iat[0]))
    ev["area_km2"] = pts.drop_duplicates(["event", "fx", "fy"]).groupby("event").size() * PIX_KM2
    ev["duration_days"] = (ev.end - ev.start).dt.days + 1
    ev["extent_km"] = haversine_km(ev.lat_min, ev.lon_min, ev.lat_max, ev.lon_max)

    # spread: farthest detection from the first-day centre, per day of duration
    first = pts[pts.date == pts.event.map(ev.start)].groupby("event")[["lat", "lon"]].mean()
    pts = pts.join(first.rename(columns={"lat": "lat0", "lon": "lon0"}), on="event")
    pts["dist0"] = haversine_km(pts.lat0, pts.lon0, pts.lat, pts.lon)
    ev["spread_km_day"] = pts.groupby("event").dist0.max() / np.maximum(ev.duration_days - 1, 1)
    ev.loc[ev.duration_days == 1, "spread_km_day"] = 0.0

    # recurrence: previous years with fire in the same ~5 km cell within +-1 month of the start month
    ev["year"], ev["month"] = ev.start.dt.year, ev.start.dt.month
    ev["rc"] = (np.round(ev.lat / REC_CELL).astype(int) * 100000 + np.round(ev.lon / REC_CELL).astype(int))
    pts["rc"] = np.round(pts.lat / REC_CELL).astype(int) * 100000 + np.round(pts.lon / REC_CELL).astype(int)
    pres = pts.assign(year=pts.date.dt.year, month=pts.date.dt.month)[["rc", "year", "month"]].drop_duplicates()
    e3 = pd.concat([ev[["rc", "year", "month"]].reset_index().assign(month=((ev.month.values - 1 + k) % 12) + 1)
                    for k in (-1, 0, 1)])
    m = e3.merge(pres.rename(columns={"year": "py_"}), on=["rc", "month"])
    m = m[m.py_ < m.year]
    prior = m.groupby("event").py_.nunique()
    avail = (ev.year - 2012).clip(lower=0)
    ev["recurrence"] = (prior.reindex(ev.index).fillna(0) / avail.where(avail > 0)).round(3)

    # type
    large = (ev.area_km2 >= LARGE_AREA_KM2) | ((ev.duration_days >= LARGE_DURATION_D) & (ev.extent_km >= LARGE_EXTENT_KM))
    small = (ev.area_km2 < SMALL_AREA_KM2) & (ev.duration_days <= SMALL_DURATION_D)
    recurrent = small & (ev.recurrence >= RECURRENT_SHARE)
    ev["type"] = np.select([large, recurrent], ["large", "recurrent"], "small")
    ev["intense"] = ev.frp_max_mw >= INTENSE_FRP_MW

    out = ev.reset_index(drop=True)
    out.insert(0, "id", np.arange(1, len(out) + 1))
    cols = ["id", "start", "end", "duration_days", "detections", "area_km2", "frp_max_mw", "frp_sum_mw",
            "night_share", "extent_km", "spread_km_day", "recurrence", "type", "intense",
            "lat", "lon", "region", "country", "year", "month"]
    out = out[cols].round({"area_km2": 2, "frp_max_mw": 1, "frp_sum_mw": 1, "night_share": 3, "extent_km": 2,
                           "spread_km_day": 2, "lat": 4, "lon": 4})
    out["start"] = out.start.dt.date
    out["end"] = out.end.dt.date
    out.to_parquet(DATA / "events.parquet", index=False, compression="zstd")

    summary = {
        "events": int(len(out)),
        "by_type": out.type.value_counts().to_dict(),
        "rules": {"link_cell_deg": CELL, "gap_days": GAP_DAYS, "pixel_km2": PIX_KM2,
                  "large": f"area >= {LARGE_AREA_KM2} km2 or (duration >= {LARGE_DURATION_D} d and extent >= {LARGE_EXTENT_KM} km)",
                  "recurrent": f"area < {SMALL_AREA_KM2} km2, duration <= {SMALL_DURATION_D} d, fire in >= {RECURRENT_SHARE:.0%} of previous years nearby",
                  "intense": f"peak FRP >= {INTENSE_FRP_MW} MW"},
        "area_km2_quantiles": out.area_km2.quantile([.5, .9, .99, .999]).round(2).to_dict(),
        "largest": out.nlargest(5, "area_km2")[["start", "region", "area_km2", "duration_days"]].astype(str).to_dict("records"),
    }
    (DATA / "events_meta.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
