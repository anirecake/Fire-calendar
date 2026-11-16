"""Save the per-region headline numbers (fires per year, trend, last month, events, risk) next to the data.

The website needs them on every first page load; computing them takes 1-2 s on a cold serverless start, and they
only change when the data are rebuilt. The file carries a stamp (data date, forecast date, number of fires and
regions); the server uses it only when the stamp matches the data it is serving, and computes otherwise.

Usage: python scripts/build_region_stats.py [site folder]     (default: this repository; run it last, after
       build_events.py and build_risk.py. scripts/stage_us.py runs it for the staged United States site.)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import app.main as m  # noqa: E402

out = m.DATA / "region_stats.json"
stats = m._compute_region_stats()
out.write_text(json.dumps({"stamp": m._stats_stamp(), "regions": stats}, ensure_ascii=False, separators=(",", ":")),
               encoding="utf-8")
print(f"wrote {out} ({len(stats)} regions, {out.stat().st_size / 1e3:.0f} kB)")
