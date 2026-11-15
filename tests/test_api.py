"""API behaviour: shapes, consistency between area types, uncertainty, validation, exports."""
import io

import numpy as np
import pandas as pd
import pytest

COUNTRIES = ["Kazakhstan", "Kyrgyzstan", "Tajikistan", "Turkmenistan", "Uzbekistan"]
BIG_BOX = {"type": "Polygon", "coordinates": [[[40, 30], [95, 30], [95, 60], [40, 60], [40, 30]]]}
FERGANA = {"type": "Polygon", "coordinates": [[[68, 40], [72, 40], [72, 43], [69, 43.5], [68, 40]]]}


def post(client, path, body):
    return client.post(path, json=body)


def cal(client, body=None):
    r = post(client, "/api/calendar", body or {})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- meta / health
def test_meta(client):
    m = client.get("/api/meta").json()
    assert m["countries"] == COUNTRIES
    assert m["date_range"][0] == "2000-11-01"
    assert {"phi", "sigma_sys", "z", "coverage_90_on_test"} <= set(m["uncertainty"])


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


# ---------------------------------------------------------------- calendar
def test_calendar_shape_and_sources(client, main):
    c = cal(client)
    n = len(main.YEARS)
    for k in ("harmonized", "harmonized_lo", "harmonized_hi", "raw_modis", "raw_viirs", "anomaly_pct", "source"):
        assert len(c[k]) == n and all(len(r) == 12 for r in c[k]), k
    yi = c["years"].index
    assert c["harmonized"][0][9] is None                    # Oct 2000: before the record
    assert c["source"][yi(2001)][5] == "modis_terra"        # Jun 2001: Terra only
    assert c["source"][yi(2005)][5] == "modis"
    assert c["source"][yi(2012)][0] == "mixed"              # VIIRS starts 20 Jan 2012
    assert c["source"][yi(2015)][5] == "viirs"
    if main.RECENT_FROM is not None:
        assert c["source"][-1][0] == "recent"
    if not main.LAST_DATE.is_month_end:                     # current partial month stays empty
        assert c["harmonized"][-1][main.LAST_DATE.month - 1] is None


def test_interval_brackets_value(client):
    c = cal(client)
    flat = lambda k: sum(c[k], [])
    for h, lo, hi, src in zip(flat("harmonized"), flat("harmonized_lo"), flat("harmonized_hi"), flat("source")):
        if h is None:
            continue
        assert lo <= h <= hi
        if src == "viirs":                                  # measured directly -> no model uncertainty
            assert lo == h == hi
        if src in ("modis", "modis_terra") and h > 50:
            assert hi > h


def test_countries_sum_to_region(client):
    total = np.array(cal(client)["annual"])
    parts = sum(np.array(cal(client, {"country": c})["annual"]) for c in COUNTRIES)
    assert np.allclose(parts, total, rtol=1e-6, atol=1)


def test_big_polygon_equals_region(client):
    a, b = cal(client), cal(client, {"geometry": BIG_BOX})
    assert np.allclose(a["annual"], b["annual"], rtol=1e-6, atol=1)


def test_point_radius_monotonic(client):
    small = cal(client, {"lat": 50.4, "lon": 80.2, "radius_km": 25})["stats"]["points"]
    large = cal(client, {"lat": 50.4, "lon": 80.2, "radius_km": 100})["stats"]["points"]
    assert 0 < small < large


def test_harmonization_removes_sensor_jump(client):
    """Raw MODIS -> VIIRS jumps ~3x at 2012; the harmonized series must not."""
    c = cal(client)
    yi = c["years"].index
    raw = (sum(c["raw_viirs"][yi(2013)]) + sum(c["raw_viirs"][yi(2014)])) / \
          (sum(c["raw_modis"][yi(2010)]) + sum(c["raw_modis"][yi(2011)]))
    harm = (c["annual"][yi(2013)] + c["annual"][yi(2014)]) / (c["annual"][yi(2010)] + c["annual"][yi(2011)])
    assert raw > 2 * harm


# ---------------------------------------------------------------- analytics
def test_analytics_full(client):
    a = post(client, "/api/analytics", {}).json()
    assert a["trend"]["n_years"] >= 20 and a["trend"]["direction"] in ("increasing", "decreasing", "no trend")
    assert 0 <= a["trend"]["p_value"] <= 1
    assert a["fire_season"]["length_months"] <= 12 and len(a["fire_season"]["risk"]) == 12
    assert a["total"]["lo90"] <= a["total"]["harmonized_hotspots"] <= a["total"]["hi90"]
    y = a["ytd"]
    assert len(y["p10"]) == len(y["p50"]) == len(y["p90"]) >= len(y["current"])
    assert all(p10 <= p50 <= p90 for p10, p50, p90 in zip(y["p10"], y["p50"], y["p90"]))


def test_analytics_year_matches_calendar(client):
    c = cal(client, {"country": "Kazakhstan"})
    a = post(client, "/api/analytics", {"country": "Kazakhstan", "start": "2019-01-01", "end": "2019-12-31"}).json()
    assert a["total"]["harmonized_hotspots"] == pytest.approx(c["annual"][c["years"].index(2019)], rel=1e-5)
    assert a["total"]["lo90"] == a["total"]["harmonized_hotspots"]   # pure VIIRS year: no model error


def test_analytics_partial_month(client):
    full = post(client, "/api/analytics", {"start": "2021-08-01", "end": "2021-08-31"}).json()["total"]
    half = post(client, "/api/analytics", {"start": "2021-08-01", "end": "2021-08-15"}).json()["total"]
    assert 0 < half["harmonized_hotspots"] < full["harmonized_hotspots"]


def test_analytics_pre_viirs_interval(client):
    a = post(client, "/api/analytics", {"start": "2005-01-01", "end": "2005-12-31"}).json()["total"]
    assert a["rescaled_modis_share"] == 1.0 and a["hi90"] > a["harmonized_hotspots"] > a["lo90"]


# ---------------------------------------------------------------- exports
def test_calendar_csv(client):
    r = post(client, "/api/calendar.csv", {"country": "Uzbekistan", "start": "2020-01-01", "end": "2020-12-31"})
    df = pd.read_csv(io.StringIO(r.text))
    assert len(df) == 12 and {"lo90", "hi90", "source", "anomaly_pct"} <= set(df.columns)


def test_hotspots_csv_small_area(client):
    r = post(client, "/api/hotspots.csv", {"geometry": FERGANA, "start": "2024-01-01", "end": "2024-12-31"})
    assert r.status_code == 200
    df = pd.read_csv(io.StringIO(r.text))
    assert len(df) > 0 and df.latitude.between(40, 43.5).all() and df.longitude.between(68, 72).all()
    assert pd.to_datetime(df.date).dt.year.eq(2024).all()


def test_hotspots_csv_too_large(client):
    assert post(client, "/api/hotspots.csv", {}).status_code == 413


def test_gzip(client):
    r = client.get("/api/grid", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_grid_filters(client):
    a = client.get("/api/grid?year=2021&month=8").json()
    assert a["year"] == 2021 and len(a["cells"]) > 0
    assert client.get("/api/grid?month=13").status_code == 400


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("body,code", [
    ({"country": "Mars"}, 400),
    ({"lat": 50, "lon": 80}, 400),                                       # radius missing
    ({"lat": 50, "lon": 80, "radius_km": 5000}, 422),                     # radius too big
    ({"start": "2020-13-01"}, 422),
    ({"geometry": {"type": "LineString", "coordinates": [[60, 40], [61, 41]]}}, 400),
    ({"geometry": {"type": "Polygon", "coordinates": [[[400, 40], [401, 40], [401, 41], [400, 40]]]}}, 400),
])
def test_bad_input(client, body, code):
    assert post(client, "/api/calendar", body).status_code == code


def test_empty_period(client):
    assert post(client, "/api/analytics", {"start": "1990-01-01", "end": "1995-01-01"}).status_code == 400


def test_too_many_vertices(client):
    a = np.linspace(0, 2 * np.pi, 6000)
    ring = np.c_[70 + np.cos(a), 45 + np.sin(a)].tolist()
    ring.append(ring[0])
    assert post(client, "/api/calendar", {"geometry": {"type": "Polygon", "coordinates": [ring]}}).status_code == 400
