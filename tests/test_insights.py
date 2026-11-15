"""Regions, fire events and the 14-day risk model."""
import json
from pathlib import Path

import numpy as np
import pytest

DATA = Path(__file__).resolve().parents[1] / "data" / "processed"


def post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- regions
def test_regions_list(client):
    r = client.get("/api/regions").json()
    assert len(r["regions"]) == 48
    kz = [x for x in r["regions"] if x["country"] == "Kazakhstan"]
    assert len(kz) == 16 and all(x["kk"] for x in kz)
    assert all("avg_per_year" in x for x in r["regions"])


def test_regions_sum_to_region_total(client):
    total = np.array(post(client, "/api/calendar", {})["annual"])
    ids = [x["id"] for x in client.get("/api/regions").json()["regions"]]
    parts = sum(np.array(post(client, "/api/calendar", {"region": i})["annual"]) for i in ids)
    assert np.allclose(parts, total, rtol=1e-6, atol=1)


def test_region_filter_and_validation(client):
    a = post(client, "/api/calendar", {"region": "KAZ-3206"})
    assert a["stats"]["points"] > 0
    assert client.post("/api/calendar", json={"region": "NOPE-1"}).status_code == 400


# ---------------------------------------------------------------- events
def test_events_basic(client):
    e = post(client, "/api/events", {"limit": 5})
    assert e["total"] > 100_000 and len(e["events"]) == 5
    areas = [x["area_km2"] for x in e["events"]]
    assert areas == sorted(areas, reverse=True)                 # default sort: biggest first
    assert set(e["by_type"]) <= {"large", "recurrent", "small"}


def test_known_fire_found(client):
    """The deadly June 2023 fire in today's Abai region (then East Kazakhstan) must be a top event."""
    e = post(client, "/api/events", {"region": "KAZ-3206", "start": "2023-06-01", "end": "2023-06-30", "limit": 1})
    top = e["events"][0]
    assert top["type"] == "large" and top["area_km2"] > 200 and top["start"].startswith("2023-06")


def test_events_filters(client):
    e = post(client, "/api/events", {"types": ["large"], "min_area_km2": 50, "country": "Kazakhstan", "limit": 500})
    assert all(x["type"] == "large" and x["area_km2"] >= 50 and x["country"] == "Kazakhstan" for x in e["events"])
    s = post(client, "/api/events", {"sort": "start", "desc": False, "limit": 3})
    assert s["events"][0]["start"] <= s["events"][1]["start"]
    r = post(client, "/api/events", {"since": "2026-11-06", "limit": 500})
    assert all(x["end"] >= "2026-11-06" for x in r["events"])
    assert client.post("/api/events", json={"types": ["volcano"]}).status_code == 400
    assert client.post("/api/events", json={"sort": "drop table"}).status_code == 422


def test_event_properties_consistent():
    import pandas as pd
    ev = pd.read_parquet(DATA / "events.parquet")
    assert (ev.duration_days >= 1).all() and (ev.area_km2 > 0).all()
    assert ((ev.night_share >= 0) & (ev.night_share <= 1)).all()
    assert (ev[ev.type == "large"].area_km2.min() >= 0)
    assert ev.recurrence.dropna().between(0, 1).all()


# ---------------------------------------------------------------- risk
def test_risk_endpoint(client):
    r = client.get("/api/risk").json()
    assert len(r["regions"]) == 48 and r["horizon_days"] == 14
    for rid, x in r["regions"].items():
        assert 0 <= x["prob"] <= 1 and x["level"] in ("low", "elevated", "high", "very_high")
        assert all(d["key"] in ("heat", "dry", "wind", "fires", "fuel", "neighbors", "lastyear") for d in x["drivers"])


def test_risk_model_beats_climatology():
    v = json.loads((DATA / "risk.json").read_text())["validation"]
    assert v["auc"] > v["auc_climatology"] + 0.1
    assert v["brier_skill_vs_climatology"] > 0.05
    assert v["ablation"]["all"]["auc"] >= v["ablation"]["season_only"]["auc"]
    # the extended model (more data) must not be worse than the first 9-input model
    assert v["ablation"]["all"]["brier_skill"] >= v["ablation"]["v1"]["brier_skill"]
    assert v["n_features"] == 18
