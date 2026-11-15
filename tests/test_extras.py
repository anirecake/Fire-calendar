"""AI assistant (with a fake Claude client, no network) and the Telegram alert bot logic."""
import json
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))


# ---------------------------------------------------------------- assistant
def test_assistant_status(client):
    s = client.get("/api/assistant/status").json()
    assert set(s) == {"llm", "model"}


def test_assistant_tools_use_real_data(main):
    from app import assistant as a
    acts = []
    r = a.run_tool("get_risk_forecast", {"region_id": "KAZ-3206"}, acts)
    assert 0 <= r["probability"] <= 1 and r["level"] in ("low", "elevated", "high", "very_high")
    top = a.run_tool("get_risk_forecast", {"country": "Uzbekistan", "top_n": 3}, acts)["top"]
    assert len(top) == 3 and all(t["country"] == "Uzbekistan" for t in top)
    assert [t["probability"] for t in top] == sorted([t["probability"] for t in top], reverse=True)
    s = a.run_tool("get_area_summary", {"region_id": "KAZ-3206"}, acts)
    assert s["total_harmonized_hotspots"]["harmonized_hotspots"] > 0
    f = a.run_tool("find_fires", {"region_id": "KAZ-3206", "start": "2023-06-01", "end": "2023-06-30", "limit": 1}, acts)
    assert f["fires"][0]["area_km2"] > 200                      # the June 2023 Abai fire
    a.run_tool("show_on_map", {"region_id": "KAZ-3206", "view": "emergency"}, acts)
    assert acts == [{"region_id": "KAZ-3206", "view": "emergency"}]
    with pytest.raises(Exception):
        a.run_tool("show_on_map", {"region_id": "NOPE-1"}, acts)


class FakeMessages:
    """Claude stand-in: first asks for the risk tool, then answers using the tool result."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return NS(stop_reason="tool_use", model="fake", content=[
                NS(type="text", text="checking"),
                NS(type="tool_use", id="t1", name="get_risk_forecast", input={"top_n": 2}),
                NS(type="tool_use", id="t2", name="show_on_map", input={"view": "emergency"})])
        res = kw["messages"][-1]["content"]
        assert res[0]["type"] == "tool_result" and "probability" in res[0]["content"]
        return NS(stop_reason="end_turn", model="fake", content=[NS(type="text", text="Highest risk: Andijan.")])


def test_assistant_loop_with_fake_claude(client, monkeypatch):
    from app import assistant as a
    fake = FakeMessages()
    monkeypatch.setattr(a, "API_KEY", "test")
    monkeypatch.setattr(a, "_client", NS(beta=NS(messages=fake)))
    r = client.post("/api/assistant", json={"messages": [{"role": "user", "content": "Где риск выше?"}], "lang": "ru",
                                            "context": {"view": "overview", "area_label": "Казахстан", "area": {"country": "Kazakhstan"}}})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["reply"] == "Highest risk: Andijan." and j["tools_used"] == ["get_risk_forecast", "show_on_map"]
    assert j["actions"] == [{"view": "emergency"}]
    first = fake.calls[0]
    assert first["model"] == a.MODEL and first["fallbacks"] == "default"
    assert "lang=ru" in first["messages"][0]["content"] and "Казахстан" in first["messages"][0]["content"]
    assert "KAZ-3206" in first["system"][0]["text"]                # region table is in the prompt


def test_assistant_input_validation(client, monkeypatch):
    from app import assistant as a
    monkeypatch.setattr(a, "API_KEY", "")
    assert client.post("/api/assistant", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 503
    monkeypatch.setattr(a, "API_KEY", "test")
    assert client.post("/api/assistant", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 400
    assert client.post("/api/assistant", json={"messages": [{"role": "system", "content": "hi"}]}).status_code == 422
    assert client.post("/api/assistant", json={"messages": [{"role": "user", "content": "x" * 1300}]}).status_code == 400


def test_links(client):
    assert "telegram_bot" in client.get("/api/links").json()


# ---------------------------------------------------------------- bot
def test_geometry_helpers():
    import firebot as fb
    assert abs(fb.haversine_km(43.24, 76.95, 43.24, 77.95) - 81.0) < 1.5          # 1 deg of longitude at 43°N
    assert fb.compass(fb.bearing(43, 77, 44, 77), "en") == "north"
    assert fb.compass(fb.bearing(43, 77, 43, 76), "ru") == "западнее"
    assert fb.parse_coords("43,24 76,95") == (43.24, 76.95)
    assert fb.parse_coords("43.24, 76.95") == (43.24, 76.95)
    assert fb.parse_coords("hello") is None
    assert fb.in_central_asia(43.24, 76.95) and not fb.in_central_asia(55.75, 37.6)
    assert [fb.word("ru", n) for n in (1, 3, 5, 11, 21)] == ["очаг", "очага", "очагов", "очагов", "очаг"]


CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight
43.300,77.000,330.1,0.4,0.4,{d},0815,N20,VIIRS,n,2.0NRT,290.1,12.5,D
43.310,77.010,320.1,0.4,0.4,{d},0815,N20,VIIRS,l,2.0NRT,290.1,3.0,D
44.900,79.000,340.1,0.4,0.4,{d},0815,N20,VIIRS,h,2.0NRT,290.1,40.0,D
"""


def test_parse_and_near():
    import firebot as fb
    now = dt.datetime.now(dt.timezone.utc)
    fires = fb.parse_firms(CSV.format(d=now.strftime("%Y-%m-%d")), "VIIRS_NOAA20_NRT")
    assert len(fires) == 2                                            # low-confidence pixel dropped
    near = fb.fires_near(fires, 43.24, 76.95, 30, now=fires[0]["when"] + dt.timedelta(hours=1))
    assert len(near) == 1 and near[0]["km"] < 10
    assert fb.fires_near(fires, 43.24, 76.95, 30, now=fires[0]["when"] + dt.timedelta(hours=30)) == []   # too old


def test_static_mask_and_region():
    import firebot as fb
    st = fb.Static()
    assert st.static, "data/processed/static_cells.json missing (scripts/build_static_mask.py)"
    y, x = next(iter(st.static))
    assert st.is_static(y / 100, x / 100) and st.is_static((y + 1) / 100, x / 100)
    assert st.region_at(43.24, 76.95) is not None                    # Almaty area
    assert st.regions[st.region_at(50.41, 80.23)]["country"] == "Kazakhstan"   # Semey


def test_store_dedupe_and_alert_text():
    import firebot as fb
    s = fb.Store(":memory:")
    s.set_lang(1, "kk")
    sub = s.subscribe(1, 43.24, 76.95)
    assert sub["radius_km"] == fb.DEFAULT_RADIUS and sub["lang"] == "kk"
    now = dt.datetime.now(dt.timezone.utc)
    fires = [{"lat": 43.30, "lon": 77.0, "when": now, "frp": 10.0, "source": "X", "key": "a"},
             {"lat": 43.32, "lon": 77.05, "when": now, "frp": 5.0, "source": "X", "key": "b"}]
    near = fb.fires_near(fires, sub["lat"], sub["lon"], sub["radius_km"])
    assert len(s.unseen(1, near)) == 2
    s.mark(1, near[:1])
    assert [f["key"] for f in s.unseen(1, near)] == ["b"]             # each fire is reported once
    for lang in ("kk", "ru", "en"):
        msg = fb.format_alert({**sub, "lang": lang}, near, fb.Static())
        assert "112" in msg and "openstreetmap.org" in msg and "UTC+5" in msg
    s.stop(1)
    assert s.get(1) is None
    s.db.close()


# ---------------------------------------------------------------- map grid for the selected area only
def test_grid_area_filter(client):
    full = client.get("/api/grid").json()["cells"]
    reg = client.get("/api/grid", params={"region": "KAZ-3206"}).json()["cells"]
    kz = client.get("/api/grid", params={"country": "Kazakhstan"}).json()["cells"]
    box = client.get("/api/grid", params={"bbox": "76,43,78,44"}).json()["cells"]
    assert 0 < len(reg) < len(kz) < len(full)
    assert box and all(42.75 <= c[0] <= 44 and 75.75 <= c[1] <= 78 for c in box)
    assert client.get("/api/grid", params={"region": "NOPE-1"}).status_code == 400
    assert client.get("/api/grid", params={"bbox": "5,4,3"}).status_code == 400


# ---------------------------------------------------------------- browser push alerts (no network: store + send mocked)
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123def456ghi789", "keys": {"p256dh": "B" * 87, "auth": "A" * 22}}


@pytest.fixture
def fake_push(monkeypatch):
    from app import push as p
    saved, sent = {}, []
    monkeypatch.setattr(p, "enabled", lambda: True)
    monkeypatch.setattr(p, "VAPID_PUBLIC", "BPUBLICKEY")
    monkeypatch.setattr(p, "store_put", lambda sid, rec: saved.__setitem__(sid, rec))
    monkeypatch.setattr(p, "store_delete", lambda sid: saved.pop(sid, None))
    monkeypatch.setattr(p, "send", lambda sub, payload, ttl=3600: sent.append(payload) or 201)
    p._hits.clear()
    return saved, sent


def test_push_subscribe_flow(client, fake_push):
    saved, sent = fake_push
    assert client.get("/api/push/config").json() == {"enabled": True, "public_key": "BPUBLICKEY"}
    r = client.post("/api/push/subscribe", json={"subscription": SUB, "lat": 43.2389, "lon": 76.8897, "radius_km": 30, "lang": "kk"})
    assert r.status_code == 200, r.text
    (sid, rec), = saved.items()
    assert rec["lat"] == 43.24 and rec["lon"] == 76.89 and rec["radius_km"] == 30     # point rounded to ~1 km
    assert set(rec) == {"sub", "lat", "lon", "radius_km", "lang", "created"}           # nothing else stored
    assert sent[0]["tag"] == "fc-welcome" and "30 км" in sent[0]["body"]
    assert client.post("/api/push/test", json={"subscription": SUB, "lat": 43.24, "lon": 76.89, "radius_km": 30, "lang": "ru"}).json()["ok"]
    assert client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).status_code == 200
    assert saved == {}


def test_push_validation(client, fake_push):
    bad = {"subscription": SUB, "lat": 55.75, "lon": 37.6, "radius_km": 30, "lang": "en"}             # Moscow
    assert client.post("/api/push/subscribe", json=bad).status_code == 400
    http = {**SUB, "endpoint": "http://insecure.example/push/123456789"}
    assert client.post("/api/push/subscribe", json={**bad, "subscription": http, "lat": 43, "lon": 77}).status_code == 422
    assert client.post("/api/push/subscribe", json={**bad, "lat": 43, "lon": 77, "radius_km": 1000}).status_code == 422


def test_service_worker_and_manifest(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "showNotification" in r.text and r.headers["service-worker-allowed"] == "/"
    assert client.get("/manifest.webmanifest").json()["short_name"] == "FireCalendar"


def test_push_alert_text():
    import firebot as fb
    now = dt.datetime.now(dt.timezone.utc)
    near = fb.fires_near([{"lat": 43.33, "lon": 77.05, "when": now, "frp": 9.0, "source": "X", "key": "k"}], 43.24, 76.95, 30)
    for lang in ("en", "ru", "kk"):
        m = fb.format_push({"lat": 43.24, "lon": 76.95, "radius_km": 30, "lang": lang}, near, fb.Static())
        assert m["title"] and "112" in m["body"] and m["url"].startswith("/#p=")


def test_push_rejected_subscription_not_stored(client, fake_push, monkeypatch):
    from app import push as p
    saved, _ = fake_push
    monkeypatch.setattr(p, "send", lambda sub, payload, ttl=3600: 502)
    r = client.post("/api/push/subscribe", json={"subscription": SUB, "lat": 43.24, "lon": 76.9, "radius_km": 30, "lang": "en"})
    assert r.status_code == 400 and saved == {}


# ---------------------------------------------------------------- place-level risk + AI briefing
def test_risk_cells(client):
    r = client.get("/api/risk/cells", params={"region": "KAZ-3206"}).json()
    assert r["cells"] and all(0 <= c["prob"] <= 1 for c in r["cells"])
    probs = [c["prob"] for c in r["cells"]]
    assert probs == sorted(probs, reverse=True)
    v = r["validation"]
    assert v["auc"] >= v["auc_place_climatology"] and v["brier_skill_vs_climatology"] > 0       # beats each place's usual season
    assert client.get("/api/risk/cells", params={"region": "NOPE-1"}).status_code == 400


class FakeBrief:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        out = {"headline": "Low risk.", "situation": "18% vs 32% usual.", "watch": [{"where": "East part (43.9, 73.6)", "why": "fires last week"}],
               "actions": [{"who": "fire units", "what": "patrol the east part", "when": "next 7 days"}], "public_message": "Do not burn grass."}
        return NS(stop_reason="end_turn", model="fake", content=[NS(type="text", text=json.dumps(out))])


def test_ai_briefing_grounded(client, monkeypatch):
    from app import assistant, briefing
    fake = FakeBrief()
    monkeypatch.setattr(assistant, "API_KEY", "test")
    monkeypatch.setattr(assistant, "_client", NS(beta=NS(messages=fake)))
    monkeypatch.setattr(briefing, "_blob_get", lambda key: None)
    monkeypatch.setattr(briefing, "_blob_put", lambda key, data: None)
    briefing._cache.clear(); briefing._hits.clear()
    b = client.get("/api/ai/briefing", params={"region": "KAZ-3252", "lang": "ru"}).json()
    assert b["headline"] == "Low risk." and b["actions"][0]["who"] == "fire units"
    kw = fake.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    facts = kw["messages"][0]["content"]
    assert "Language: Russian" in facts and "province_forecast_14d" in facts and "place_forecast_14d" in facts
    client.get("/api/ai/briefing", params={"region": "KAZ-3252", "lang": "ru"})
    assert len(fake.calls) == 1                                                                   # cached
    assert client.get("/api/ai/briefing", params={"region": "NOPE-1", "lang": "ru"}).status_code == 400


def test_risk_cells_country(client):
    r = client.get("/api/risk/cells", params={"country": "Uzbekistan"}).json()
    assert r["cells"] and all(c["region"].startswith("UZB") for c in r["cells"])
    assert client.get("/api/risk/cells").status_code == 400
