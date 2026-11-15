"""Invariants of the processed dataset and the harmonization model (model.json)."""
import json
from pathlib import Path

import duckdb
import pytest

DATA = Path(__file__).resolve().parents[1] / "data" / "processed"


@pytest.fixture(scope="module")
def meta():
    return json.loads((DATA / "model.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def db():
    con = duckdb.connect()
    con.execute(f"CREATE VIEW p AS SELECT * FROM read_parquet('{(DATA / 'points.parquet').as_posix()}')")
    return con


def q(db, sql):
    return db.execute(sql).fetchone()[0]


def test_modis_weight_zero_once_viirs_flies(db, meta):
    assert q(db, f"SELECT count(*) FROM p WHERE sensor='MODIS' AND date >= DATE '{meta['viirs_start']}' AND w <> 0") == 0


def test_modis_weight_positive_before_viirs(db, meta):
    assert q(db, f"SELECT min(w) FROM p WHERE sensor='MODIS' AND date < DATE '{meta['viirs_start']}'") > 0


def test_viirs_weights(db):
    assert q(db, "SELECT count(*) FROM p WHERE sensor='VIIRS' AND satellite='Suomi NPP' AND w <> 1") == 0
    # NOAA-20 only fills Suomi NPP outages, scaled close to 1
    assert q(db, "SELECT coalesce(max(abs(w - 1)), 0) FROM p WHERE satellite='NOAA-20'") < 0.3


def test_no_viirs_before_launch(db, meta):
    assert q(db, f"SELECT count(*) FROM p WHERE sensor='VIIRS' AND date < DATE '{meta['viirs_start']}'") == 0


def test_points_inside_central_asia(db):
    assert q(db, "SELECT count(*) FROM p WHERE lat NOT BETWEEN 35 AND 56 OR lon NOT BETWEEN 46 AND 88") == 0


def test_model_beats_baselines(meta):
    v = meta["validation"]
    for lvl in ("region_monthly", "block_1deg_monthly"):
        assert v[lvl]["ours"]["r2_log"] >= v[lvl]["const"]["r2_log"] > v[lvl]["raw"]["r2_log"]
        assert abs(v[lvl]["ours"]["bias_pct"]) < 5


def test_interval_coverage_honest(meta):
    c = meta["uncertainty"]["coverage_90_on_test"]
    assert 0.8 <= c["block_1deg_monthly"] <= 0.97
    assert 0.8 <= c["region_monthly"] <= 1.0


def test_ratio_stable_before_orbit_drift(meta):
    r = {int(k): v for k, v in meta["annual_ratio"].items()}
    stable = [r[y] for y in range(2013, 2022)]
    assert max(stable) - min(stable) < 0.5
