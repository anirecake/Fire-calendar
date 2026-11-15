"""Assemble the United States site for deployment: same code, US data.

Copies the app into ../fire-calendar-usa with data/processed_us as data/processed and the US states and
border files as web/regions.geojson and web/countries.geojson. Deploy that folder as its own Vercel project:
  python scripts/stage_us.py && cd ../fire-calendar-usa && vercel deploy --prod
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT.parent / "fire-calendar-usa"
SKIP_DATA = {"weather.parquet"}                       # only needed to train the models


def main() -> None:
    DEST.mkdir(exist_ok=True)
    for d in ("app", "api", "web"):
        if (DEST / d).exists():
            shutil.rmtree(DEST / d)
        shutil.copytree(ROOT / d, DEST / d, ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("vercel.json", "requirements.txt", ".python-version", ".vercelignore"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, DEST / f)
    for f in ("regions.geojson", "countries.geojson"):
        shutil.copy2(ROOT / "data" / "web_us" / f, DEST / "web" / f)
    proc = DEST / "data" / "processed"
    if proc.exists():
        shutil.rmtree(proc)
    proc.mkdir(parents=True)
    for f in (ROOT / "data" / "processed_us").iterdir():
        if f.name in SKIP_DATA:
            continue
        if f.name == "points.parquet":                 # ~9 M hotspots: compact types + strong zstd to fit the function size
            import duckdb
            src, dst = f.as_posix(), (proc / f.name).as_posix()
            duckdb.connect().execute(f"""
                COPY (SELECT lat::FLOAT lat, lon::FLOAT lon, date, time, sensor, satellite, conf, frp::FLOAT frp, daynight,
                             country, vscale::FLOAT vscale, year::SMALLINT AS "year", month::TINYINT AS "month", cx, cy, w::FLOAT w, region
                      FROM read_parquet('{src}') ORDER BY region, date)
                TO '{dst}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 19, ROW_GROUP_SIZE 250000)""")
            print(f"points.parquet {f.stat().st_size / 1e6:.0f} MB -> {(proc / f.name).stat().st_size / 1e6:.0f} MB")
            continue
        shutil.copy2(f, proc / f.name)
    size = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file() and ".vercel" not in f.parts)
    print(f"staged {DEST} ({size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
