"""After-fire reports: fact sheets for large fires (built by scripts/build_fire_reports.py) and an AI write-up.

GET /api/fires                      list of fires with a report (filter by region, country or bbox)
GET /api/fires/{id}                 the full fact sheet: footprint growth, land cover, settlements and roads around,
                                    weather vs usual, rank in history, NISAR radar before/after (where available)
GET /api/fires/{id}/files/{name}    radar images (before/after/change) and SARWatch PDF reports
GET /api/ai/fire-report?id=&lang=   Claude turns the fact sheet into two texts: a news-style summary for
                                    journalists and an after-action note for emergency services. Numbers and
                                    names come only from the fact sheet (structured JSON output). Cached per fire
                                    and language (memory + private Vercel Blob).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter()
ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "data" / "processed" / "fires"
_index_file = DIR / "index.json"
INDEX = json.loads(_index_file.read_text(encoding="utf-8")) if _index_file.exists() else {"built": None, "fires": []}
BY_ID = {f["id"]: f for f in INDEX["fires"]}
FILES = {"before.png": "image/png", "after.png": "image/png", "delta.png": "image/png",
         "report-en.pdf": "application/pdf", "report-ru.pdf": "application/pdf", "report-kk.pdf": "application/pdf"}
_cache: dict[str, dict] = {}
_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def detail(fid: int) -> dict:
    if fid not in BY_ID:
        raise HTTPException(404, "no report for this fire")
    return json.loads((DIR / f"{fid}.json").read_text(encoding="utf-8"))


@router.get("/api/fires", summary="Large fires with an after-fire report (filter by region, country or bbox=w,s,e,n)")
def fires(region: str | None = None, country: str | None = None, bbox: str | None = None, limit: int = 50):
    out = INDEX["fires"]
    if region:
        out = [f for f in out if f["region"] == region]
    if country:
        out = [f for f in out if f["country"] == country]
    if bbox:
        try:
            w, s, e, n = map(float, bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be w,s,e,n")
        out = [f for f in out if w <= f["lon"] <= e and s <= f["lat"] <= n]
    return {"built": INDEX["built"], "total": len(out), "fires": out[:max(1, min(limit, 200))]}


@router.get("/api/fires/{fid}", summary="After-fire fact sheet for one fire")
def fire(fid: int):
    return detail(fid)


@router.get("/api/fires/{fid}/files/{name}", summary="Radar images and SARWatch PDF reports of a fire")
def fire_file(fid: int, name: str):
    p = DIR / str(fid) / name
    if fid not in BY_ID or name not in FILES or not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p, media_type=FILES[name], headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------------- AI write-up
SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "news headline, max 14 words, factual, no drama"},
        "summary": {"type": "string", "description": "3-5 sentences for journalists: what burned, where, when, how big, "
                                                     "how unusual, with numbers from the facts"},
        "key_numbers": {"type": "array", "description": "4-6 headline numbers",
                        "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                                  "required": ["label", "value"], "additionalProperties": False}},
        "why": {"type": "string", "description": "2-3 sentences: conditions that helped the fire (weather vs usual, fuel, "
                                                 "land type), cautious wording"},
        "impact": {"type": "string", "description": "2-3 sentences: what was affected: land types and areas, settlements "
                                                    "nearby with distances, roads or power lines crossing the area; say "
                                                    "plainly what the data cannot tell (damage to buildings, casualties)"},
        "radar": {"type": "string", "description": "1-2 sentences on the NISAR radar check, or empty string if no radar data"},
        "for_emergency": {"type": "object", "properties": {
            "assessment": {"type": "string", "description": "2-3 sentences after-action assessment for the emergency service"},
            "lessons": {"type": "array", "items": {"type": "string"}, "description": "2-4 lessons tied to facts"},
            "actions": {"type": "array", "description": "3-5 concrete actions for next season",
                        "items": {"type": "object", "properties": {
                            "who": {"type": "string"}, "what": {"type": "string"}, "when": {"type": "string"}},
                            "required": ["who", "what", "when"], "additionalProperties": False}}},
            "required": ["assessment", "lessons", "actions"], "additionalProperties": False},
        "caveats": {"type": "array", "items": {"type": "string"}, "description": "2-3 limits of the data in plain words"},
    },
    "required": ["headline", "summary", "key_numbers", "why", "impact", "radar", "for_emergency", "caveats"],
    "additionalProperties": False,
}
LANG = {"en": "English", "ru": "Russian", "kk": "Kazakh"}
SYSTEM = """You write after-fire reports from satellite data for journalists and for {people} in {region}.
Emergency number for residents: {num}.
Use ONLY the facts in the user message:
- Every number, date, settlement, road and land type must come from the facts. Never invent casualties, damaged
  buildings, money losses, causes, organisations or quotes. Settlement names: use the name in the requested
  language if given, else the local name as written.
- footprint_km2 is the ACTIVE-FIRE FOOTPRINT from VIIRS 375 m detections, not a mapped burned area; call it
  "area with active fire detections" or similar, and say it approximates the burned area.
- modis_burned.burned_km2 is NASA's MODIS MCD64A1 burned-area map (500 m) next to this fire: the best single
  burned-area figure when available and missing_months is empty; if months are missing, say it may be too low.
  calibration = km2 burned per km2 of VIIRS footprint over many fires in this region (use it for "expected" area).
- Weather is a province average from NASA POWER; compare with the usual values given.
- Radar (NISAR L-band, via SARWatch) is experimental: signal "clear" = radar shows a distinct drop inside the fire
  area compared with the surroundings; "weak" = small difference; "none" = radar does not separate the burned area
  (common on grassland and when the dates are weeks apart). If radar is null, return an empty string for radar.
- Rank: rank_region 1 = largest fire in the province since the "since" date.
- Plain, short sentences. No markdown, no emojis. Write in the requested language."""


def ai_facts(rep: dict) -> dict:
    import app.main as core
    reg = core.REGIONS.get(rep["region"], {})
    f = {k: v for k, v in rep.items() if k not in ("points", "bbox")}
    f["province"] = {"id": rep["region"], "en": reg.get("en"), "ru": reg.get("ru"), "kk": reg.get("kk"), "country": rep["country"]}
    if f.get("radar"):
        f["radar"] = {k: v for k, v in f["radar"].items() if k not in ("bounds", "images", "reports", "range_db")}
    f["meaning"] = {"night_share": "share of detections at night (fire kept burning at night)",
                    "frp_max_mw": "peak fire radiative power, MW (intensity)",
                    "spread_km_day": "farthest detection from the first-day centre per day",
                    "growth": "day by day: detections and new footprint area",
                    "around.places": "settlements within 15 km of the fire footprint, km = distance to the nearest detection",
                    "history.share_recurrent_nearby": "share of previous years with fire at the same place in the same season"}
    return f


@router.get("/api/ai/fire-report", summary="AI after-fire report: summary for journalists + after-action note for emergency services")
def fire_report(id: int, request: Request, lang: str = "en"):
    from app import assistant, briefing
    if lang not in LANG:
        raise HTTPException(400, "lang must be en, ru or kk")
    rep = detail(id)
    key = f"ai/fire/{INDEX['built']}/{id}_{lang}.json"
    if key in _cache:
        return _cache[key]
    cached = briefing._blob_get(key)
    if cached:
        _cache[key] = cached
        return cached
    if not assistant.API_KEY:
        raise HTTPException(503, "AI not configured")
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    with _lock:
        q = [t for t in _hits.get(ip, []) if t > now - 600]
        if len(q) >= 12:
            raise HTTPException(429, "too many requests")
        _hits[ip] = q + [now]
    import anthropic
    import app.main as core
    try:
        resp = assistant.client().beta.messages.create(
            model=assistant.MODEL, max_tokens=4000,
            system=SYSTEM.format(people=core.DOMAIN.get("ai_people", "the regional emergency service"),
                                 region=core.DOMAIN.get("ai_region", ""), num=core.DOMAIN.get("emergency_number", "112")),
            messages=[{"role": "user", "content": f"Language: {LANG[lang]}.\nFacts (JSON):\n"
                                                  f"{json.dumps(ai_facts(rep), ensure_ascii=False, default=str)}"}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.RateLimitError:
        raise HTTPException(429, "AI is busy, try again in a minute")
    except (anthropic.APIConnectionError, anthropic.APITimeoutError):
        raise HTTPException(503, "AI unreachable")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"AI error {e.status_code}")
    if resp.stop_reason == "refusal":
        raise HTTPException(502, "AI declined")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        raise HTTPException(502, "AI returned no report")
    out = {"id": id, "lang": lang, "model": resp.model, **data,
           "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    _cache[key] = out
    briefing._blob_put(key, out)
    return out
