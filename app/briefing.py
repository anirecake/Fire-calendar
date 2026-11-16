"""AI situation briefing for a regional emergency service (Claude, grounded in our data).

Generic advice ("keep equipment ready") does not help a provincial emergency department. This endpoint
collects everything FireCalendar knows about one province — the 14-day risk model and its drivers, the
place-level (~25 km cell) forecast, weather versus normal, fires of the last two weeks, the province's
fire history — and asks Claude to turn exactly these facts into a short briefing: what is happening,
which parts of the province to watch and why, concrete actions (who / what / when) and a warning text
for residents. Numbers come only from the facts; the response is JSON (structured output).

Cached per province, language and forecast date (memory + private Vercel Blob), so a province is
analysed once per forecast, not on every page view.
"""
from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
_cache: dict[str, dict] = {}

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "one sentence: the situation in this province for the next 14 days"},
        "situation": {"type": "string", "description": "2-4 sentences: risk level and the main reasons, with numbers from the facts"},
        "watch": {"type": "array", "description": "2-4 parts of the province to watch, from the place-level forecast and recent fires",
                  "items": {"type": "object", "properties": {
                      "where": {"type": "string", "description": "part of the province + coordinates from the facts"},
                      "why": {"type": "string"}}, "required": ["where", "why"], "additionalProperties": False}},
        "actions": {"type": "array", "description": "4-6 concrete actions that follow from the facts",
                    "items": {"type": "object", "properties": {
                        "who": {"type": "string", "description": "which service or group, e.g. fire units, local authorities, farmers, forestry"},
                        "what": {"type": "string"},
                        "when": {"type": "string", "description": "timing, e.g. 'next 3 days', 'before the spring season (March)'"}},
                        "required": ["who", "what", "when"], "additionalProperties": False}},
        "public_message": {"type": "string", "description": "2-3 sentence warning or advice for residents, plain words"},
    },
    "required": ["headline", "situation", "watch", "actions", "public_message"],
    "additionalProperties": False,
}

LANG = {"en": "English", "ru": "Russian", "kk": "Kazakh"}
SYSTEM_TMPL = """You are a fire-risk analyst preparing a briefing for {people} in {region}.
The emergency phone number for residents is {num}.
Use ONLY the facts in the user message. Rules:
- Every number, date and place must come from the facts. Never invent settlement names, organisations,
  phone numbers, equipment counts or statistics. Refer to places as parts of the province (e.g. "north-east
  part") together with the coordinates given in the facts.
- Be specific and practical: tie each action to a fact (a high-probability cell, recurrent agricultural burns,
  dry or windy weather, a quiet season that is good for preparation, a past large fire in the same weeks).
- If the risk is low, say so plainly and recommend what is useful now (preparation before the next season,
  monitoring the specific cells that still show activity), not alarm.
- Satellites see heat, not causes: write "likely" when you attribute fires to agricultural or pasture burning.
- Short sentences. No markdown, no emojis. Write in the requested language."""


def _core():
    import app.main as core
    return core


def _part(lat: float, lon: float, bounds: tuple[float, float, float, float]) -> str:
    """Which part of the province a point is in (north-east, centre, ...), from the province bounding box."""
    w, s, e, n = bounds
    fy = (lat - s) / max(n - s, 1e-6)
    fx = (lon - w) / max(e - w, 1e-6)
    ns = "north" if fy > 0.62 else "south" if fy < 0.38 else ""
    ew = "east" if fx > 0.62 else "west" if fx < 0.38 else ""
    return f"{ns}-{ew}".strip("-") or "centre"


@lru_cache(maxsize=1)
def _region_geoms() -> dict:
    """Province shapes, parsed once per server instance."""
    import shapely
    feats = json.loads((_core().WEB / "regions.geojson").read_text(encoding="utf-8"))["features"]
    return {f["properties"]["id"]: shapely.geometry.shape(f["geometry"]) for f in feats}


def facts(rid: str) -> dict:
    core = _core()
    if rid not in core.REGIONS:
        raise HTTPException(400, f"unknown region '{rid}'")
    reg = core.REGIONS[rid]
    geom = _region_geoms().get(rid)
    b = geom.bounds if geom is not None else (0, 0, 1, 1)
    risk = core.RISK["regions"].get(rid) if core.RISK else None
    out = {"province": {"id": rid, "name_en": reg["en"], "name_ru": reg["ru"], "name_kk": reg["kk"], "country": reg["country"],
                        "bounds_lon_lat": [round(x, 2) for x in b], "approx_area_km2": round(geom.area * 111 * 85) if geom is not None else None},
           "data_through": str(core.LAST_DATE.date())}
    if risk:
        out["province_forecast_14d"] = {
            "issue_date": core.RISK["issue_date"], "definition": core.RISK["definition"],
            "probability": risk["prob"], "usual_probability_this_time_of_year": risk["usual_prob"], "level": risk["level"],
            "drivers_risk_increase": {d["key"]: d["push"] for d in risk["drivers"]},
            "driver_meaning": "fires = fires already burning; dry = little rain / dry air / dry soil; heat; wind; fuel = rain 3-6 months ago grew grass; neighbors = fires in neighbouring provinces; lastyear = many fires in the same weeks last year",
            "weather_last_30d_vs_normal": risk["now"], "similar_past_situations": risk["analogs"],
            "model_skill": {k: core.RISK["validation"][k] for k in ("auc", "auc_climatology", "hit_rate_when_high", "event_rate_test")}}
    cells = core.RISK_CELLS["regions"].get(rid, []) if core.RISK_CELLS else []
    if cells:
        top = [c for c in cells if c[2] >= 0.05][:6] or cells[:3]
        out["place_forecast_14d"] = {
            "definition": core.RISK_CELLS["definition"], "cells_in_province": len(cells),
            "cells_by_level": {lvl: sum(1 for c in cells if _lvl(c[2]) == lvl) for lvl in ("very_high", "high", "elevated", "low")},
            "top_cells": [{"lat": c[0] + 0.125, "lon": c[1] + 0.125, "part_of_province": _part(c[0] + 0.125, c[1] + 0.125, b),
                           "probability": c[2], "fires_last_14d": c[3], "fires_same_14d_last_year": c[4], "drivers": c[5]} for c in top],
            "driver_meaning": "place_fires = fires in this cell recently; neighbours = fires in surrounding cells; last_year = fires here in the same weeks last year; place_season = this place usually burns at this time; weather / province_fires = province-wide conditions"}
    since = (core.LAST_DATE - dt.timedelta(days=14)).date()
    ev = core.events(core.EventQuery(region=rid, since=str(since), sort="area_km2", limit=5))
    out["fires_last_14_days"] = {
        "since": str(since), "count": ev["total"], "by_type": ev["by_type"], "total_footprint_km2": ev["area_km2_total"],
        "type_meaning": "large = wildfire; recurrent = small repeated burn at the same place, likely agricultural; small = other",
        "largest": [{"start": e["start"], "end": e["end"], "area_km2": e["area_km2"], "type": e["type"],
                     "part_of_province": _part(e["lat"], e["lon"], b), "lat": round(e["lat"], 2), "lon": round(e["lon"], 2)} for e in ev["events"][:3]]}
    st = core._region_stats().get(rid, {})
    out["history"] = {k: st.get(k) for k in ("avg_per_year", "last5_per_year", "trend_pct_decade", "trend_p", "peak_month", "last_month",
                                               "events_recent", "biggest_event")}
    out["today_month"] = core.LAST_DATE.strftime("%B")
    return out


def _lvl(p: float) -> str:
    return "very_high" if p >= 0.5 else "high" if p >= 0.25 else "elevated" if p >= 0.1 else "low"


def _blob_key(rid: str, lang: str, issue: str) -> str:
    return f"ai/briefing/{issue}/{rid}_{lang}.json"


def _blob_get(key: str) -> dict | None:
    try:
        from app import push
        if not push.blob_ready():
            return None
        from vercel.blob import sync as blob
        return json.loads(blob.get_blob(key, access="private").body)
    except Exception:
        return None


def _blob_put(key: str, data: dict) -> None:
    try:
        from app import push
        if push.blob_ready():
            from vercel.blob import sync as blob
            blob.put_blob(key, json.dumps(data, ensure_ascii=False).encode(), access="private",
                          content_type="application/json", allow_overwrite=True)
    except Exception:
        pass


@router.get("/api/ai/briefing", summary="AI briefing for a province: situation, places to watch, concrete actions (Claude + our data)")
def briefing(region: str, request: Request, lang: str = "en"):
    from app import assistant
    if lang not in LANG:
        raise HTTPException(400, "lang must be en, ru or kk")
    core = _core()
    if region not in core.REGIONS:
        raise HTTPException(400, f"unknown region '{region}'")
    issue = core.RISK["issue_date"] if core.RISK else "none"
    key = _blob_key(region, lang, issue)
    if key in _cache:
        return _cache[key]
    cached = _blob_get(key)                             # saved briefing first: the facts are only built on a miss
    if cached:
        _cache[key] = cached
        return cached
    f = facts(region)
    data, model = assistant.json_call(
        request,
        SYSTEM_TMPL.format(people=core.DOMAIN.get("ai_people", "the regional emergency service"),
                           region=core.DOMAIN.get("ai_region", ""), num=core.DOMAIN.get("emergency_number", "112")),
        f"Language: {LANG[lang]}.\nFacts (JSON):\n{json.dumps(f, ensure_ascii=False, default=str)}", SCHEMA, "briefing")
    out = {"region": region, "lang": lang, "issue_date": issue, "model": model, **data,
           "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    _cache[key] = out
    _blob_put(key, out)
    return out
