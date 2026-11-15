"""Which part of the world the pipeline builds: FC_DOMAIN=ca (Central Asia, default) or FC_DOMAIN=us (United States).

Every script imports its paths and lists from here, so the same code builds both sites:
  ca  raw -> data/raw,     processed -> data/processed,     regions/borders -> web/
  us  raw -> data/raw_us,  processed -> data/processed_us,  regions/borders -> data/web_us/
The US site is deployed from a staging copy where data/processed_us becomes data/processed (scripts/stage_us.py).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = os.environ.get("FC_DOMAIN", "ca").lower()

if DOMAIN == "us":
    COUNTRIES = ["United States"]                    # FIRMS / Natural Earth admin name
    FIRMS_NAMES = {"United States": "United_States"}  # FIRMS archive file names
    NE_NAMES = {"United States": "United States of America"}   # Natural Earth admin names
    BBOX = (-180.0, 17.5, -64.0, 72.0)               # west, south, east, north (CONUS, Alaska, Hawaii, Puerto Rico)
    RAW = ROOT / "data" / "raw_us"
    DATA = ROOT / "data" / "processed_us"
    WEB_OUT = ROOT / "data" / "web_us"
else:
    COUNTRIES = ["Kazakhstan", "Uzbekistan", "Kyrgyzstan", "Tajikistan", "Turkmenistan"]
    FIRMS_NAMES = {c: c for c in COUNTRIES}
    NE_NAMES = {c: c for c in COUNTRIES}
    BBOX = (46.0, 35.0, 88.0, 56.0)
    RAW = ROOT / "data" / "raw"
    DATA = ROOT / "data" / "processed"
    WEB_OUT = ROOT / "web"

BBOX_STR = ",".join(str(int(x)) if float(x).is_integer() else str(x) for x in BBOX)
FIRMS_TO_ADMIN = {v: k for k, v in FIRMS_NAMES.items()}
NE_TO_ADMIN = {v: k for k, v in NE_NAMES.items()}
