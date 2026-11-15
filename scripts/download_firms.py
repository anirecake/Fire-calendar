"""Download NASA FIRMS yearly country hotspot archives (MODIS C6.1 + VIIRS S-NPP).

Files come from the public FIRMS country archive:
  https://firms.modaps.eosdis.nasa.gov/data/country/<sensor>/<year>/<sensor>_<year>_<Country>.csv

Usage:
  python scripts/download_firms.py                 # all Central Asia, all years
  python scripts/download_firms.py --countries Kazakhstan --years 2020 2021
"""
from __future__ import annotations

import argparse
import gzip
import concurrent.futures as cf
import datetime as dt
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://firms.modaps.eosdis.nasa.gov/data/country"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import domain  # noqa: E402

ROOT = domain.ROOT
RAW = domain.RAW

CENTRAL_ASIA = domain.COUNTRIES          # name kept for older imports: the countries of the current domain

# sensor folder name -> first year available in the archive
SENSORS = {"modis": 2000, "viirs-snpp": 2012}


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:  # use the OS trust store when available (helps behind corporate/AV proxies)
        import truststore  # type: ignore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    return ctx


CTX = _ssl_context()


def fetch(sensor: str, year: int, country: str, retries: int = 4) -> str:
    name = f"{sensor}_{year}_{domain.FIRMS_NAMES.get(country, country)}.csv"
    dest = RAW / sensor / name
    big = domain.DOMAIN == "us"                     # big archives are stored gzip-compressed (DuckDB reads .csv.gz)
    if big:
        dest = dest.with_name(name + ".gz")
    if dest.exists() and dest.stat().st_size > 0:
        return f"skip  {name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}/{sensor}/{year}/{name}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, context=CTX, timeout=120) as r:
                data = r.read()
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(gzip.compress(data, 6) if big else data)
            tmp.replace(dest)
            return f"ok    {name} ({len(data) / 1e6:.1f} MB)"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return f"none  {name} (not in archive)"
            err = f"HTTP {e.code}"
        except Exception as e:  # network hiccup -> retry
            err = repr(e)
        time.sleep(2 * attempt)
    return f"FAIL  {name}: {err}"


def main() -> int:
    last_full_year = dt.date.today().year - 1
    p = argparse.ArgumentParser()
    p.add_argument("--countries", nargs="+", default=CENTRAL_ASIA)
    p.add_argument("--years", nargs="+", type=int)
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args()

    jobs = []
    for sensor, first in SENSORS.items():
        years = a.years or range(first, last_full_year + 1)
        for y in years:
            if y < first:
                continue
            for c in a.countries:
                jobs.append((sensor, y, c))

    print(f"{len(jobs)} files to check -> {RAW}", flush=True)
    failed = 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for msg in ex.map(lambda j: fetch(*j), jobs):
            print(msg, flush=True)
            failed += msg.startswith("FAIL")
    print(f"done, {failed} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
