"""Point -> admin-1 region lookup (web/regions.geojson)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import domain  # noqa: E402

ROOT = domain.ROOT
REGIONS = domain.WEB_OUT / "regions.geojson"


def load_regions():
    fc = json.loads(REGIONS.read_text(encoding="utf-8"))
    ids = np.array([f["properties"]["id"] for f in fc["features"]], dtype=object)
    geoms = np.array([shape(f["geometry"]) for f in fc["features"]], dtype=object)
    return fc, ids, geoms


def assign(lat: np.ndarray, lon: np.ndarray, country: np.ndarray | None = None) -> np.ndarray:
    """Region id for each point. Points in simplification gaps go to the nearest region
    (of the same country when `country` is given)."""
    fc, ids, geoms = load_regions()
    tree = shapely.STRtree(geoms)
    pts = shapely.points(np.asarray(lon, float), np.asarray(lat, float))
    out = np.full(len(pts), None, dtype=object)
    ip, ig = tree.query(pts, predicate="intersects")
    out[ip] = ids[ig]
    miss = np.where(out == None)[0]  # noqa: E711
    if len(miss):
        if country is None:
            ig = tree.query_nearest(pts[miss], return_distance=False)[1]
            out[miss] = ids[ig]
        else:
            ctry = np.array([f["properties"]["country"] for f in fc["features"]], dtype=object)
            for c in np.unique(country[miss]):
                sel = miss[country[miss] == c]
                cand = np.where(ctry == c)[0] if c in set(ctry) else np.arange(len(ids))
                sub = shapely.STRtree(geoms[cand])
                ig = sub.query_nearest(pts[sel], return_distance=False)[1]
                out[sel] = ids[cand][ig]
    return out
