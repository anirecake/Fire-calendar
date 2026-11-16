"""AI assistant: Claude answers questions in plain language, using OUR data and OUR risk model as tools.

The language model never invents numbers: every figure comes from a tool call into the same code
that serves the website (harmonized calendar, analytics, fire events, the 14-day gradient-boosting
risk model). Claude decides which tool to call, reads the result and explains it.

Needs ANTHROPIC_API_KEY in the environment (Vercel project settings or .env locally).
Without it, /api/assistant/status reports llm=false and the web widget falls back to offline
answers built from data the page already has.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.env import env

ROOT = Path(__file__).resolve().parents[1]
MODEL = os.environ.get("ASSISTANT_MODEL", "claude-opus-5-5")
MAX_TURNS = 6                      # tool-use rounds per question
MAX_HISTORY = 12                   # messages kept from the conversation
MAX_CHARS = 1200                   # per user message
RATE_PER_IP = (15, 600)            # 15 questions per 10 minutes per visitor
RATE_GLOBAL = (400, 3600)          # 400 questions per hour per server instance (cost guard)

router = APIRouter()


API_KEY = env("ANTHROPIC_API_KEY")
_client = None
BUDGET_S = 50.0                    # whole request must end before the 60 s serverless limit
CALL_TIMEOUT_S = 40.0


def client():
    """No SDK retries: a retry could double the wait and pass the 60 s function limit. Overload is handled by
    the server-side model fallback; a failed call returns a clear error and the visitor asks again."""
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=API_KEY, timeout=CALL_TIMEOUT_S, max_retries=0)
    return _client


# --------------------------------------------------------------------------- rate limiting
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _allow(key: str, limit: int, window: int) -> bool:
    now = time.time()
    with _lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()


def json_call(request: Request, system: str, content: str, schema: dict, what: str = "answer") -> tuple[dict, str]:
    """One grounded Claude call that must return JSON matching `schema` (AI briefing, after-fire report).
    Rate-limited per visitor and per server instance; errors mapped to HTTP codes. Returns (data, model)."""
    if not API_KEY:
        raise HTTPException(503, "AI not configured")
    if not _allow(f"json:{client_ip(request)}", 12, 600) or not _allow("global", *RATE_GLOBAL):
        raise HTTPException(429, "too many requests")
    import anthropic
    try:
        resp = client().beta.messages.create(
            model=MODEL, max_tokens=4000, system=system, messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
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
        return json.loads(text), resp.model
    except ValueError:
        raise HTTPException(502, f"AI returned no {what}")


# --------------------------------------------------------------------------- tools
def _core():
    import app.main as core          # lazy: avoids a circular import (main mounts this router)
    return core


def _name(rid: str) -> dict:
    p = _core().REGIONS.get(rid, {})
    return {"id": rid, "en": p.get("en"), "ru": p.get("ru"), "kk": p.get("kk"), "country": p.get("country")}


def _area(inp: dict):
    core = _core()
    keys = ("country", "lat", "lon", "radius_km", "start", "end")
    a = {k: inp[k] for k in keys if inp.get(k) not in (None, "")}
    if inp.get("region_id"):
        a["region"] = inp["region_id"]
    area = core.Area(**a)
    core.resolve(area)                 # unknown country / region / incomplete point -> HTTPException(400)
    return area


def tool_risk(inp: dict) -> dict:
    core = _core()
    risk = core.RISK
    if not risk:
        return {"error": "risk model not available"}
    v = risk["validation"]
    head = {"issue_date": risk["issue_date"], "horizon_days": risk["horizon_days"],
            "definition": risk["definition"], "levels": risk["levels"],
            "model_skill": {"auc": v["auc"], "auc_climatology": v["auc_climatology"],
                            "hit_rate_when_high": v["hit_rate_when_high"], "base_rate": v["event_rate_test"]}}
    rid = inp.get("region_id")
    if rid:
        if rid not in risk["regions"]:
            return {"error": f"unknown region_id {rid}"}
        r = risk["regions"][rid]
        return {**head, "region": _name(rid), "probability": r["prob"], "level": r["level"],
                "usual_probability_this_time_of_year": r["usual_prob"], "times_usual": r["times_usual"],
                "drivers": [{"factor": d["key"], "risk_drop_if_normal": d["push"]} for d in r["drivers"]],
                "conditions_now_vs_normal": r["now"], "past_analogs": r["analogs"][:3],
                "driver_keys": "fires=fires already burning, fuel=rain of last 3-6 months (grass grown), dry=dryness, "
                               "heat=heat, wind=wind, neighbors=fires in neighbouring regions, lastyear=same weeks last year"}
    items = [(k, r) for k, r in risk["regions"].items()
             if not inp.get("country") or core.REGIONS.get(k, {}).get("country") == inp["country"]]
    items.sort(key=lambda kv: -kv[1]["prob"])
    n = max(1, min(int(inp.get("top_n") or 5), 15))
    counts: dict[str, int] = {}
    for _, r in items:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    return {**head, "regions_by_level": counts,
            "top": [{**_name(k), "probability": r["prob"], "level": r["level"], "times_usual": r["times_usual"],
                     "main_driver": r["drivers"][0]["key"] if r["drivers"] else None} for k, r in items[:n]]}


def tool_summary(inp: dict) -> dict:
    core = _core()
    area = _area(inp)
    cal = core.calendar(area)
    an = core.analytics(area)
    st = cal["stats"]
    tr, trv = an["trend"], an["trend_viirs_only"]
    fs = an["fire_season"]
    months = core.MONTHS
    return {
        "data_through": str(core.LAST_DATE.date()),
        "period": an["period"],
        "total_harmonized_hotspots": an["total"],
        "trend_harmonized": tr and {k: tr[k] for k in ("pct_per_decade", "p_value", "direction", "significant_95", "n_years")},
        "trend_viirs_only_for_comparison": trv and {k: trv[k] for k in ("pct_per_decade", "p_value", "significant_95")},
        "fire_season": fs and {"months": fs["label"], "peak_month": months[fs["peak_month"] - 1], "share_of_year": fs["share"]},
        "latest_full_month": st["latest"],
        "this_month_so_far_vs_same_days_in_past_years": st["month_to_date"],
        "year_to_date_vs_median_pct": an["ytd"] and an["ytd"]["current_vs_median_pct"],
        "top_years": st["top_years"],
        "annual_last_6": an["annual"][-6:],
        "note": "hotspots = satellite fire detections harmonized to VIIRS sensitivity (one fire can give many hotspots)",
    }


def tool_fires(inp: dict) -> dict:
    core = _core()
    a = _area(inp).model_dump()
    q = core.EventQuery(**a, types=inp.get("types") or None, min_area_km2=float(inp.get("min_area_km2") or 0),
                        since=inp.get("since") or None, sort=inp.get("sort") or "area_km2",
                        limit=max(1, min(int(inp.get("limit") or 5), 10)))
    r = core.events(q)
    keep = ("start", "end", "duration_days", "area_km2", "frp_max_mw", "type", "lat", "lon", "night_share", "spread_km_day")
    return {"total_matching": r["total"], "by_type": r["by_type"], "total_area_km2": r["area_km2_total"],
            "type_meaning": "large = wildfire (>=10 km2 or long and wide), recurrent = small repeated burn, likely agricultural, small = other",
            "area_meaning": "active-fire footprint from 375 m VIIRS pixels, not the mapped burned area",
            "fires": [{**{k: e[k] for k in keep}, "region": _name(e["region"]) if e.get("region") else None} for e in r["events"]]}


AREA_PROPS = {
    "country": {"type": "string", "description": "country name exactly as in the region table"},
    "region_id": {"type": "string", "description": "admin-1 region id from the region table, e.g. KAZ-3206"},
    "lat": {"type": "number"}, "lon": {"type": "number"},
    "radius_km": {"type": "number", "description": "with lat/lon: circle radius, 1-500 km"},
}
TOOLS = [
    {"name": "get_risk_forecast",
     "description": "14-day fire-activity risk from our gradient-boosting model (NASA POWER weather + FIRMS fires). "
                    "With region_id: probability, level, drivers, weather now vs normal, past analog years. "
                    "Without: regions ranked by risk (optionally within one country).",
     "input_schema": {"type": "object", "properties": {
         "region_id": AREA_PROPS["region_id"], "country": AREA_PROPS["country"],
         "top_n": {"type": "integer", "minimum": 1, "maximum": 15}}}},
    {"name": "get_area_summary",
     "description": "Harmonized MODIS+VIIRS hotspot statistics for an area (the whole site area if no area given): "
                    "total for the period with 90% interval, trend per decade (Mann-Kendall), fire season, "
                    "latest month and this month so far vs the multi-year median, top years.",
     "input_schema": {"type": "object", "properties": {
         **AREA_PROPS, "start": {"type": "string", "description": "YYYY-MM-DD"}, "end": {"type": "string", "description": "YYYY-MM-DD"}}}},
    {"name": "find_fires",
     "description": "Individual fires (VIIRS hotspots linked within 1 km and 2 days) with area, duration, peak power (MW), type. "
                    "Use since=YYYY-MM-DD for fires burning recently; sort by area_km2, frp_max_mw, duration_days or start.",
     "input_schema": {"type": "object", "properties": {
         **AREA_PROPS, "start": {"type": "string"}, "end": {"type": "string"},
         "since": {"type": "string", "description": "only fires still burning on/after this date"},
         "types": {"type": "array", "items": {"type": "string", "enum": ["large", "recurrent", "small"]}},
         "min_area_km2": {"type": "number"},
         "sort": {"type": "string", "enum": ["area_km2", "frp_max_mw", "duration_days", "start"]},
         "limit": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "show_on_map",
     "description": "Move the user's map and report to an area (region, country or point+radius) and optionally switch "
                    "the view: overview (public), emergency (risk + actions), analyst (tables, fires). Use when the "
                    "user asks to show/open something, or when it clearly helps.",
     "input_schema": {"type": "object", "properties": {
         **AREA_PROPS, "view": {"type": "string", "enum": ["overview", "emergency", "analyst"]}}}},
]


def run_tool(name: str, inp: dict, actions: list) -> dict:
    if name == "get_risk_forecast":
        return tool_risk(inp)
    if name == "get_area_summary":
        return tool_summary(inp)
    if name == "find_fires":
        return tool_fires(inp)
    if name == "show_on_map":
        _area(inp)                                   # validates ids / coordinates
        act = {k: inp[k] for k in ("region_id", "country", "lat", "lon", "radius_km", "view") if inp.get(k) is not None}
        actions.append(act)
        return {"ok": True, "shown": act}
    return {"error": f"unknown tool {name}"}


# --------------------------------------------------------------------------- prompt
def system_prompt() -> str:
    core = _core()
    m, rv = core.META, (core.RISK or {}).get("validation", {})
    regions = "\n".join(f"{rid}: {p['en']} / {p['ru']} / {p['kk']} ({p['country']})" for rid, p in core.REGIONS.items())
    val = m.get("validation", {})
    dom = core.DOMAIN
    num = dom.get("emergency_number", "112")
    return f"""You are the assistant inside FireCalendar, a NASA Space Apps 2026 web app about vegetation fires in
{dom.get("ai_region")}.

What the app does:
- Harmonizes NASA FIRMS MODIS (2000→) and VIIRS (2012→) active-fire hotspots into one consistent series, so the
  sensor no longer matters (MODIS rescaled to VIIRS sensitivity per 0.25° cell and month; validated out of sample).
- Burning-activity calendar (year × month), anomalies vs the multi-year median, trends, fire season.
- Fire events: hotspots linked into individual fires with area, duration, power (FRP), type.
- AI 14-day risk forecast per region: gradient boosting with 18 inputs (NASA POWER weather: heat, humidity,
  air dryness, rain 1 week to 6 months, soil moisture, wind, sunshine; fires already burning; neighbours; last
  year; season). Hindcast 2019–2025: AUC {rv.get('auc')} vs {rv.get('auc_climatology')} for climatology; when it says
  "high", above-normal fires followed {round(100 * rv.get('hit_rate_when_high', 0))}% of the time vs
  {round(100 * rv.get('event_rate_test', 0))}% normally. {dom.get("ai_causes", "")}
- Three views: overview (public), emergency (risk, why, what to do), analyst (tables, fire explorer, CSV).
- Downloads: monthly CSV, raw hotspots CSV, charts as PNG. Point search: type coordinates or click the map.
- Data through {core.LAST_DATE.date()}; risk forecast issued {(core.RISK or {}).get('issue_date')}.
- Harmonization validation: {json.dumps(val)[:400]}

Regions (id: English / Russian / Kazakh (country)). Use these ids in tools:
{regions}

Rules:
- Every number you state must come from a tool result in this conversation. If a tool cannot answer, say so.
- Answer in the language given by the `lang` note (en, ru or kk = Kazakh). Use the region names of that language.
- Be short and concrete: 2–6 sentences or a few bullets, plain words, no tables unless asked. Explain "hotspot"
  (a satellite fire detection) when it matters. Give the date the data refer to.
- You are not an emergency service. If someone reports a fire or danger to life, tell them to call {num} first.
- Satellites see heat, not cause; give likely causes as likely, not certain.
- Use show_on_map when the user wants to see something or when an answer is about one specific area.
- Off-topic requests: answer in one line and steer back to fires, data and the app."""


# --------------------------------------------------------------------------- endpoint
class Msg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class AskBody(BaseModel):
    messages: list[Msg] = Field(min_length=1)
    lang: str = Field("en", pattern="^(en|ru|kk)$")
    context: dict[str, Any] | None = None


@router.get("/api/assistant/status", summary="Is the language-model assistant enabled?")
def status():
    return {"llm": bool(API_KEY), "model": MODEL if API_KEY else None}


def _context_note(body: AskBody) -> str:
    ctx = body.context or {}
    parts = [f"lang={body.lang}", f"today's data run through {_core().LAST_DATE.date()}"]
    if ctx.get("view"):
        parts.append(f"user is in the '{ctx['view']}' view")
    if ctx.get("area_label"):
        parts.append(f"selected area: {str(ctx['area_label'])[:80]} {json.dumps(ctx.get('area') or {})[:200]}")
    loc = ctx.get("location")
    if isinstance(loc, dict) and isinstance(loc.get("lat"), (int, float)) and isinstance(loc.get("lon"), (int, float)):
        parts.append(f"user's own location (shared by the browser): lat {loc['lat']:.3f}, lon {loc['lon']:.3f}")
    return "[context: " + "; ".join(parts) + "]"


@router.post("/api/assistant", summary="Ask the AI assistant (Claude + FireCalendar data tools)")
def ask(body: AskBody, request: Request):
    if not API_KEY:
        raise HTTPException(503, "assistant not configured (no ANTHROPIC_API_KEY)")
    if not _allow(f"ip:{client_ip(request)}", *RATE_PER_IP) or not _allow("global", *RATE_GLOBAL):
        raise HTTPException(429, "too many questions, try again in a few minutes")
    import anthropic

    hist = [m for m in body.messages if m.content.strip()][-MAX_HISTORY:]
    while hist and hist[0].role != "user":
        hist = hist[1:]
    if not hist or hist[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")
    if len(hist[-1].content) > MAX_CHARS:
        raise HTTPException(400, f"question longer than {MAX_CHARS} characters")
    messages: list[dict] = [{"role": m.role, "content": m.content} for m in hist[:-1]]
    messages.append({"role": "user", "content": f"{_context_note(body)}\n\n{hist[-1].content}"})

    actions: list[dict] = []
    used: list[str] = []
    sys_blocks = [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}]
    started = time.monotonic()
    try:
        for _ in range(MAX_TURNS):
            left = BUDGET_S - (time.monotonic() - started)
            if left < 6:                                        # not enough time for another model round
                return {"reply": None, "error": "timeout", "actions": actions, "tools_used": used}
            resp = client().beta.messages.create(
                model=MODEL, max_tokens=2048, system=sys_blocks, tools=TOOLS, messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                timeout=min(CALL_TIMEOUT_S, left - 2),
            )
            if resp.stop_reason == "refusal":
                return {"reply": None, "refused": True, "actions": actions, "tools_used": used}
            if resp.stop_reason != "tool_use":
                text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                return {"reply": text, "actions": actions, "tools_used": used, "model": resp.model}
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                used.append(b.name)
                try:
                    out, err = run_tool(b.name, dict(b.input or {}), actions), False
                except HTTPException as e:
                    out, err = {"error": e.detail}, True
                except Exception as e:                          # bad arguments -> let the model correct itself
                    out, err = {"error": f"{type(e).__name__}: {str(e)[:300]}"}, True
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str), "is_error": err})
            messages.append({"role": "user", "content": results})
        return {"reply": None, "error": "too many steps", "actions": actions, "tools_used": used}
    except anthropic.AuthenticationError:
        raise HTTPException(503, "assistant key rejected")
    except anthropic.RateLimitError:
        raise HTTPException(429, "assistant is busy, try again in a minute")
    except (anthropic.APIConnectionError, anthropic.APITimeoutError):
        raise HTTPException(503, "assistant unreachable")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"assistant error {e.status_code}")
