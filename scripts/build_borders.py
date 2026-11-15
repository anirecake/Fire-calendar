"""Country outlines for the map: Natural Earth 1:50m admin-0 (public domain), five countries,
simplified, written to web/countries.geojson.

Usage: python scripts/build_borders.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import shapely
from shapely.geometry import mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_firms import CTX, ROOT  # noqa: E402
import domain  # noqa: E402

URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
OUT = domain.WEB_OUT / "countries.geojson"


def main() -> None:
    with urllib.request.urlopen(URL, context=CTX, timeout=120) as r:
        world = json.loads(r.read().decode("utf-8"))
    feats = []
    for f in world["features"]:
        name = f["properties"].get("ADMIN") or f["properties"].get("NAME")
        if name not in domain.NE_TO_ADMIN:
            continue
        g = shape(f["geometry"]).intersection(shapely.box(*domain.BBOX))
        g = shapely.set_precision(g.simplify(0.01, preserve_topology=True), 0.001)
        feats.append({"type": "Feature", "properties": {"name": domain.NE_TO_ADMIN[name]}, "geometry": mapping(g)})
    missing = set(domain.COUNTRIES) - {f["properties"]["name"] for f in feats}
    if missing:
        sys.exit(f"missing countries: {missing}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e3:.0f} kB, {len(feats)} countries)")


if __name__ == "__main__":
    main()
