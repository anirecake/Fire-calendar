"""Browser push alerts (Web Push): works in any country, no Telegram needed.

Flow
  1. The page registers /sw.js (service worker), asks permission and creates a push subscription with our
     public VAPID key, then POSTs it with a point and radius to /api/push/subscribe.
  2. The subscription is stored in a PRIVATE Vercel Blob store (push/<id>.json): endpoint + keys, point rounded
     to 0.01° (~1 km), radius, language. Nothing else about the person is stored.
  3. The alert loop (bot/firebot.py, every ~30 min) reads the subscriptions, checks NASA FIRMS near-real-time
     fires and sends a push for new fires inside each radius. Expired subscriptions are deleted.
  The server also sends a welcome push right after subscribing and a sample alert on request, so the whole
  chain can be checked immediately.

Env: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY (base64url), BLOB_READ_WRITE_TOKEN (or Vercel OIDC on Vercel).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
_dom = ROOT / "data" / "processed" / "domain.json"
DOMAIN = json.loads(_dom.read_text(encoding="utf-8")) if _dom.exists() else {}
BBOX = tuple(DOMAIN.get("bbox", (46.0, 35.0, 88.0, 56.0)))   # west, south, east, north of this site
PREFIX = "push/"
router = APIRouter()


from app.env import env as _env  # noqa: E402

VAPID_PUBLIC = _env("VAPID_PUBLIC_KEY")
VAPID_PRIVATE = _env("VAPID_PRIVATE_KEY")
if not os.environ.get("BLOB_READ_WRITE_TOKEN") and _env("BLOB_READ_WRITE_TOKEN"):
    os.environ["BLOB_READ_WRITE_TOKEN"] = _env("BLOB_READ_WRITE_TOKEN")      # local dev: from .env.local
CONTACT = "https://fire-calendar.vercel.app"           # VAPID "subject": the site itself, no personal data

TEXT = {
    "en": {"welcome_t": "FireCalendar alerts are on",
           "welcome_b": "You will get a notification when satellites see a new fire within {r} km of your point.",
           "demo_t": "New fire near you (sample)",
           "demo_b": "Satellite detected 2 fires within {r} km. Closest: 12 km north-east, 40 min ago. If you see smoke or flames call 112."},
    "ru": {"welcome_t": "Оповещения FireCalendar включены",
           "welcome_b": "Вы получите уведомление, когда спутники увидят новый пожар в радиусе {r} км от вашей точки.",
           "demo_t": "Новый пожар рядом с вами (пример)",
           "demo_b": "Спутник заметил 2 очага в радиусе {r} км. Ближайший — в 12 км к северо-востоку, 40 минут назад. Видите дым или огонь — звоните 112."},
    "kk": {"welcome_t": "FireCalendar хабарламалары қосылды",
           "welcome_b": "Серіктер нүктеңізден {r} км радиуста жаңа өрт көргенде хабарлама аласыз.",
           "demo_t": "Жаныңызда жаңа өрт (үлгі)",
           "demo_b": "Серік {r} км радиуста 2 ошақ байқады. Ең жақыны — солтүстік-шығыста 12 км, 40 минут бұрын. Түтін не от көрсеңіз — 112-ге қоңырау шалыңыз."},
}


def blob_ready() -> bool:
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("BLOB_STORE_ID"))


def enabled() -> bool:
    return DOMAIN.get("alerts", True) and bool(VAPID_PUBLIC and VAPID_PRIVATE and (os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("BLOB_STORE_ID")))


def sub_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:24]


def send(subscription: dict, payload: dict, ttl: int = 3600) -> int:
    """Send one push; returns the push service HTTP status (201 ok, 404/410 = subscription is gone)."""
    from pywebpush import WebPushException, webpush
    try:
        r = webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=VAPID_PRIVATE, vapid_claims={"sub": CONTACT}, ttl=ttl, timeout=15)
        return getattr(r, "status_code", 201)
    except WebPushException as e:
        return e.response.status_code if e.response is not None else 502
    except Exception:                                  # unreachable / bogus endpoint
        return 502


# --------------------------------------------------------------------------- storage (private Vercel Blob)
def store_put(sid: str, record: dict) -> None:
    from vercel.blob import sync as blob
    blob.put_blob(f"{PREFIX}{sid}.json", json.dumps(record).encode(), access="private",
                  content_type="application/json", allow_overwrite=True)


def store_delete(sid: str) -> None:
    from vercel.blob import sync as blob
    try:
        blob.delete_blob(f"{PREFIX}{sid}.json")
    except Exception:
        pass


def store_all() -> list[dict]:
    """All subscriptions (used by the alert loop)."""
    from vercel.blob import sync as blob
    out = []
    for item in blob.query_blobs(prefix=PREFIX):
        try:
            rec = json.loads(blob.get_blob(item.pathname, access="private").body)
            rec["id"] = item.pathname[len(PREFIX):-5]
            out.append(rec)
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- API
class Keys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class Subscription(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)
    keys: Keys
    expirationTime: float | None = None

    @field_validator("endpoint")
    @classmethod
    def _https(cls, v):
        if not v.startswith("https://"):
            raise ValueError("endpoint must be https")
        return v


class SubscribeBody(BaseModel):
    subscription: Subscription
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(30, ge=5, le=150)
    lang: str = Field("en", pattern="^(en|ru|kk)$")


class EndpointBody(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)
    lang: str = Field("en", pattern="^(en|ru|kk)$")


_hits: dict[str, list[float]] = {}


def _limit(request: Request, n: int = 20, window: int = 600) -> None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    q = [t for t in _hits.get(ip, []) if t > now - window]
    if len(q) >= n:
        raise HTTPException(429, "too many requests")
    q.append(now)
    _hits[ip] = q


@router.get("/api/push/config", summary="Browser push alerts: enabled? + public VAPID key")
def config():
    return {"enabled": enabled(), "public_key": VAPID_PUBLIC if enabled() else None}


@router.post("/api/push/subscribe", summary="Subscribe a browser to fire alerts around a point")
def subscribe(body: SubscribeBody, request: Request):
    if not enabled():
        raise HTTPException(503, "push alerts not configured")
    _limit(request)
    w, s, e, n = BBOX
    if not (s <= body.lat <= n and w <= body.lon <= e):
        raise HTTPException(400, "point must be inside the area this site covers")
    sub = body.subscription.model_dump(exclude_none=True)
    sid = sub_id(sub["endpoint"])
    record = {"sub": sub, "lat": round(body.lat, 2), "lon": round(body.lon, 2), "radius_km": round(body.radius_km),
              "lang": body.lang, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    t = TEXT[body.lang]
    status = send(sub, {"title": t["welcome_t"], "body": t["welcome_b"].format(r=record["radius_km"]),
                        "url": f"/#p={record['lat']},{record['lon']},{min(200, max(10, record['radius_km']))}", "tag": "fc-welcome"})
    if status >= 300:                                  # only subscriptions that really received the welcome push are kept
        raise HTTPException(400, f"push service rejected the subscription ({status})")
    store_put(sid, record)
    return {"ok": True, "id": sid, "lat": record["lat"], "lon": record["lon"], "radius_km": record["radius_km"],
            "welcome_push_status": status}


@router.post("/api/push/unsubscribe", summary="Stop fire alerts for this browser")
def unsubscribe(body: EndpointBody, request: Request):
    if not enabled():
        raise HTTPException(503, "push alerts not configured")
    _limit(request)
    store_delete(sub_id(body.endpoint))
    return {"ok": True}


@router.post("/api/push/test", summary="Send a sample fire alert to this browser")
def test(body: SubscribeBody, request: Request):
    if not enabled():
        raise HTTPException(503, "push alerts not configured")
    _limit(request, n=10)
    t = TEXT[body.lang]
    sub = body.subscription.model_dump(exclude_none=True)
    status = send(sub, {"title": t["demo_t"], "body": t["demo_b"].format(r=round(body.radius_km)),
                        "url": f"/#p={body.lat:.2f},{body.lon:.2f},{min(200, max(10, round(body.radius_km)))}", "tag": "fc-demo"})
    return {"ok": status < 300, "status": status}
