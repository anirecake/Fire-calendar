"""Compact list of static heat sources (gas flares, plants, volcano-like vents) for live alerts.

Near-real-time FIRMS data has no `type` column, so the Telegram bot cannot tell a gas flare
from a wildfire. The archive can: every 0.01 deg cell where VIIRS flagged >= MIN_N detections as
type 1/2 (volcano / other static land source) in past years is written to
data/processed/static_cells.json and masked (with its 8 neighbours) by the bot.

Usage: python scripts/build_static_mask.py
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed" / "static_cells.json"
MIN_N = 5


def main() -> None:
    files = [(RAW / s).as_posix() + "/*.csv" for s in ("viirs-snpp", "modis")]
    src = " UNION ALL ".join(
        f"SELECT latitude, longitude, type FROM read_csv('{f}', union_by_name=true)" for f in files)
    rows = duckdb.connect().execute(f"""
        SELECT round(latitude*100)::INT AS sy, round(longitude*100)::INT AS sx, count(*) AS n
        FROM ({src}) WHERE type IN (1, 2) GROUP BY ALL HAVING count(*) >= {MIN_N} ORDER BY 1, 2
    """).fetchall()
    OUT.write_text(json.dumps({"cell_deg": 0.01, "min_n": MIN_N, "cells": [[y, x] for y, x, _ in rows]}),
                   encoding="utf-8")
    print(f"{len(rows):,} static cells -> {OUT}")


if __name__ == "__main__":
    main()
