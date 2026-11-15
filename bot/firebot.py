"""FireCalendar Telegram bot: warns people when satellites see a new fire near them.

How it works
  1. A person sends the bot their location (or types "43.24, 76.95") and picks a radius.
  2. Every POLL_MINUTES the bot downloads the latest NASA FIRMS near-real-time detections for Central
     Asia (VIIRS NOAA-20 / NOAA-21 / S-NPP 375 m + MODIS 1 km, last 24 h), drops known gas flares and
     plants (static-source mask from the archive) and low-confidence pixels.
  3. Each subscriber gets one message listing NEW detections inside their radius: how many, how far,
     in which direction, when, a map link, the 14-day risk for their region, and what to do.

Run (needs Python deps from requirements-dev.txt):
  set TELEGRAM_BOT_TOKEN=...   (from @BotFather)  - or put it in fire-calendar/.env
  python bot/firebot.py                  # long polling + alert loop (Ctrl+C to stop)
  python bot/firebot.py --test-fetch     # only download FIRMS and print what would count as fires
The FIRMS key is the same FIRMS_MAP_KEY used by scripts/download_recent.py.
Telegram subscriptions live in bot/firebot.db (SQLite). Browser (Web Push) subscriptions made on the website
live in the private Vercel Blob store (see app/push.py); this loop alerts both.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("FIREBOT_DB", Path(__file__).with_name("firebot.db")))
FIRMS_API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
BBOX = "46,35,88,56"                       # west,south,east,north - all five countries
SOURCES = ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT"]
POLL_MINUTES = float(os.environ.get("FIREBOT_POLL_MINUTES", 30))
DEFAULT_RADIUS = 30
RADII = [10, 30, 50, 100]
MAX_AGE_H = 24                             # detections older than this are never alerted
SITE = os.environ.get("FIREBOT_SITE", "https://fire-calendar.vercel.app").rstrip("/")


# --------------------------------------------------------------------------- config
def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    f = ROOT / ".env"
    if not v and f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return v


def ssl_ctx() -> ssl.SSLContext:
    try:                                       # OS trust store (antivirus / corporate proxies)
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


CTX = ssl_ctx()


# --------------------------------------------------------------------------- geometry
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.radians
    y = math.sin(r(lon2 - lon1)) * math.cos(r(lat2))
    x = math.cos(r(lat1)) * math.sin(r(lat2)) - math.sin(r(lat1)) * math.cos(r(lat2)) * math.cos(r(lon2 - lon1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg: float, lang: str) -> str:
    return T[lang]["dirs"][int((deg + 22.5) // 45) % 8]


# --------------------------------------------------------------------------- static data
class Static:
    """Static heat sources, regions and the 14-day risk forecast (all from data/processed + web)."""

    def __init__(self) -> None:
        p = ROOT / "data" / "processed" / "static_cells.json"
        self.static = {tuple(c) for c in json.loads(p.read_text())["cells"]} if p.exists() else set()
        rp = ROOT / "data" / "processed" / "risk.json"
        self.risk = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else None
        self.regions, self.tree, self.geoms = {}, None, []
        gp = ROOT / "web" / "regions.geojson"
        if gp.exists():
            import shapely
            from shapely.geometry import shape
            feats = json.loads(gp.read_text(encoding="utf-8"))["features"]
            self.geoms = [shape(f["geometry"]) for f in feats]
            self.ids = [f["properties"]["id"] for f in feats]
            self.regions = {f["properties"]["id"]: f["properties"] for f in feats}
            self.tree = shapely.STRtree(self.geoms)

    def is_static(self, lat: float, lon: float) -> bool:
        y, x = round(lat * 100), round(lon * 100)
        return any((y + a, x + b) in self.static for a in (-1, 0, 1) for b in (-1, 0, 1))

    def region_at(self, lat: float, lon: float) -> str | None:
        if self.tree is None:
            return None
        import shapely
        pt = shapely.Point(lon, lat)
        for i in self.tree.query(pt, predicate="intersects"):
            return self.ids[int(i)]
        i = self.tree.nearest(pt)                       # just outside a border -> nearest region (≤ ~30 km)
        return self.ids[int(i)] if self.geoms[int(i)].distance(pt) < 0.3 else None


# --------------------------------------------------------------------------- FIRMS
def parse_firms(text: str, source: str) -> list[dict]:
    """FIRMS area CSV -> list of detections with a stable key; low-confidence pixels dropped."""
    if not text.strip() or "latitude" not in text.splitlines()[0]:
        return []
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        conf = str(r.get("confidence", "")).strip().lower()
        if source.startswith("VIIRS") and conf in ("l", "low"):
            continue
        if source.startswith("MODIS") and conf.isdigit() and int(conf) < 30:
            continue
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
            t = str(r.get("acq_time", "0")).zfill(4)
            when = dt.datetime.strptime(f"{r['acq_date']} {t}", "%Y-%m-%d %H%M").replace(tzinfo=dt.timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append({"lat": lat, "lon": lon, "when": when, "frp": float(r.get("frp") or 0), "source": source,
                    "key": f"{source[:5]}:{lat:.3f},{lon:.3f}@{when:%Y%m%d%H%M}"})
    return out


def fetch_fires(key: str, static: Static, days: int = 1) -> list[dict]:
    fires = []
    for src in SOURCES:
        url = f"{FIRMS_API}/{key}/{src}/{BBOX}/{days}"
        try:
            with urllib.request.urlopen(url, context=CTX, timeout=90) as r:
                text = r.read().decode("utf-8")
        except Exception as e:                           # one sensor down must not stop the others
            print(f"[firms] {src}: {e}", flush=True)
            continue
        if text.startswith("Invalid") or "Exceeding allowed" in text:
            print(f"[firms] {src}: {text[:120]}", flush=True)
            continue
        fires += parse_firms(text, src)
    return [f for f in fires if not static.is_static(f["lat"], f["lon"])]


def fires_near(fires: list[dict], lat: float, lon: float, radius_km: float, now: dt.datetime | None = None) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for f in fires:
        if (now - f["when"]).total_seconds() > MAX_AGE_H * 3600:
            continue
        d = haversine_km(lat, lon, f["lat"], f["lon"])
        if d <= radius_km:
            out.append({**f, "km": d, "deg": bearing(lat, lon, f["lat"], f["lon"])})
    return sorted(out, key=lambda f: f["km"])


# --------------------------------------------------------------------------- texts
T = {
    "ru": {
        "dirs": ["севернее", "северо-восточнее", "восточнее", "юго-восточнее", "южнее", "юго-западнее", "западнее", "северо-западнее"],
        "start": "<b>FireCalendar — оповещения о пожарах</b>\n\nЯ слежу за спутниковыми данными NASA FIRMS по Центральной Азии и напишу вам, когда рядом с вами появится новый очаг.\n\n1. Нажмите «Отправить геолокацию» (или напишите координаты, например <code>43.24, 76.95</code>).\n2. Выберите радиус.\n\nКоманды: /status — что сейчас рядом, /risk — прогноз риска, /radius — изменить радиус, /stop — отписаться, /demo — пример оповещения.",
        "send_loc": "Отправить геолокацию",
        "choose_lang": "Тілді таңдаңыз · Выберите язык · Choose language",
        "subscribed": "Подписка оформлена: радиус <b>{r} км</b> вокруг точки {lat:.3f}, {lon:.3f}{region}.\nПроверяю спутники примерно каждые {m} минут.",
        "in_region": " ({name})",
        "radius_q": "Радиус оповещений:",
        "now_none": "Сейчас в радиусе {r} км за последние 24 часа очагов не видно.",
        "now_some": "За последние 24 часа в радиусе {r} км: <b>{n}</b> {word}. Ближайший — {km:.0f} км {dir}.",
        "fires_word": ["очаг", "очага", "очагов"],
        "risk_line": "Прогноз на 14 дней для области «{name}»: <b>{lvl}</b> ({p}% вероятность пожаров выше нормы, обычно {u}%).",
        "lvl": {"low": "низкий", "elevated": "повышенный", "high": "высокий", "very_high": "очень высокий"},
        "alert": "<b>Новый пожар рядом с вами</b>\n\nСпутник заметил <b>{n}</b> {word} в радиусе {r} км. Ближайший — <b>{km:.0f} км {dir}</b> от вашей точки, {time}.\n{list}\n<a href=\"{map}\">Карта</a> · <a href=\"{site}\">FireCalendar</a>\n\n{risk}\n\nЕсли видите дым или огонь — звоните <b>112</b>. Не жгите траву и мусор, держитесь подальше от горящей степи и тростника, следите за ветром.",
        "alert_item": "• {km:.0f} км {dir}, {time}, мощность {frp:.0f} МВт",
        "time_fmt": "{d}.{m:02d} в {hh:02d}:{mm:02d} ({tz})",
        "demo": "<b>Это пример (демо)</b> — так выглядит настоящее оповещение:",
        "stopped": "Подписка отменена. Чтобы снова получать оповещения, отправьте геолокацию.",
        "no_sub": "Вы ещё не подписаны. Отправьте геолокацию ",
        "bad": "Не понял. Отправьте геолокацию или координаты вида <code>43.24, 76.95</code>.",
        "outside": "Эта точка вне Центральной Азии — я слежу только за Казахстаном, Кыргызстаном, Таджикистаном, Туркменистаном и Узбекистаном.",
        "top_risk": "Самый высокий риск пожаров на 14 дней (прогноз от {d}):",
        "unsub_btn": "Отписаться",
        "src": "Данные: NASA FIRMS (VIIRS, MODIS). Спутники видят тепло, а не причину; облака и дым могут скрывать очаги.",
        "push_title": "Новый пожар рядом с вами",
        "push_body": "{n} {word} в радиусе {r} км. Ближайший — {km:.0f} км {dir}, {time}. Видите дым или огонь — звоните 112.",
    },
    "kk": {
        "dirs": ["солтүстікте", "солтүстік-шығыста", "шығыста", "оңтүстік-шығыста", "оңтүстікте", "оңтүстік-батыста", "батыста", "солтүстік-батыста"],
        "start": "<b>FireCalendar — өрт туралы хабарламалар</b>\n\nМен Орталық Азия бойынша NASA FIRMS серіктік деректерін бақылаймын және жаныңызда жаңа өрт ошағы пайда болса, сізге жазамын.\n\n1. «Геолокацияны жіберу» батырмасын басыңыз (немесе координаттарды жазыңыз, мысалы <code>43.24, 76.95</code>).\n2. Радиусты таңдаңыз.\n\nПәрмендер: /status — қазір жанымда не бар, /risk — қауіп болжамы, /radius — радиусты өзгерту, /stop — жазылымнан шығу, /demo — хабарлама үлгісі.",
        "send_loc": "Геолокацияны жіберу",
        "choose_lang": "Тілді таңдаңыз · Выберите язык · Choose language",
        "subscribed": "Жазылым рәсімделді: {lat:.3f}, {lon:.3f} нүктесінің айналасында <b>{r} км</b> радиус{region}.\nСеріктерді шамамен әр {m} минут сайын тексеремін.",
        "in_region": " ({name})",
        "radius_q": "Хабарлама радиусы:",
        "now_none": "Қазір {r} км радиуста соңғы 24 сағатта ошақ көрінбейді.",
        "now_some": "Соңғы 24 сағатта {r} км радиуста: <b>{n}</b> ошақ. Ең жақыны — {km:.0f} км {dir}.",
        "fires_word": ["ошақ", "ошақ", "ошақ"],
        "risk_line": "«{name}» облысына 14 күндік болжам: <b>{lvl}</b> (өрттің нормадан жоғары болу ықтималдығы {p}%, әдетте {u}%).",
        "lvl": {"low": "төмен", "elevated": "жоғарылаған", "high": "жоғары", "very_high": "өте жоғары"},
        "alert": "<b>Жаныңызда жаңа өрт</b>\n\nСерік {r} км радиуста <b>{n}</b> ошақ байқады. Ең жақыны — нүктеңізден <b>{km:.0f} км {dir}</b>, {time}.\n{list}\n<a href=\"{map}\">Карта</a> · <a href=\"{site}\">FireCalendar</a>\n\n{risk}\n\nТүтін не от көрсеңіз — <b>112</b>-ге қоңырау шалыңыз. Шөп пен қоқысты өртемеңіз, жанып жатқан дала мен қамыстан алыс болыңыз, желді қадағалаңыз.",
        "alert_item": "• {km:.0f} км {dir}, {time}, қуаты {frp:.0f} МВт",
        "time_fmt": "{d}.{m:02d}, сағат {hh:02d}:{mm:02d} ({tz})",
        "demo": "<b>Бұл үлгі (демо)</b> — нақты хабарлама осылай көрінеді:",
        "stopped": "Жазылым тоқтатылды. Хабарламаларды қайта алу үшін геолокацияны жіберіңіз.",
        "no_sub": "Сіз әлі жазылмағансыз. Геолокацияны жіберіңіз ",
        "bad": "Түсінбедім. Геолокацияны немесе <code>43.24, 76.95</code> түріндегі координаттарды жіберіңіз.",
        "outside": "Бұл нүкте Орталық Азиядан тыс — мен тек Қазақстанды, Қырғызстанды, Тәжікстанды, Түрікменстанды және Өзбекстанды бақылаймын.",
        "top_risk": "14 күндік өрт қаупі ең жоғары облыстар ({d} болжамы):",
        "unsub_btn": "Жазылымнан шығу",
        "src": "Деректер: NASA FIRMS (VIIRS, MODIS). Серіктер себепті емес, жылуды көреді; бұлт пен түтін ошақтарды жасыруы мүмкін.",
        "push_title": "Жаныңызда жаңа өрт",
        "push_body": "{r} км радиуста {n} ошақ. Ең жақыны — {km:.0f} км {dir}, {time}. Түтін не от көрсеңіз — 112-ге қоңырау шалыңыз.",
    },
    "en": {
        "dirs": ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"],
        "start": "<b>FireCalendar fire alerts</b>\n\nI watch NASA FIRMS satellite fire detections over Central Asia and message you when a new fire appears near you.\n\n1. Tap “Send location” (or type coordinates, e.g. <code>43.24, 76.95</code>).\n2. Pick a radius.\n\nCommands: /status — what is near me now, /risk — risk forecast, /radius — change radius, /stop — unsubscribe, /demo — sample alert.",
        "send_loc": "Send location",
        "choose_lang": "Тілді таңдаңыз · Выберите язык · Choose language",
        "subscribed": "Subscribed: <b>{r} km</b> around {lat:.3f}, {lon:.3f}{region}.\nI check the satellites about every {m} minutes.",
        "in_region": " ({name})",
        "radius_q": "Alert radius:",
        "now_none": "No fires seen within {r} km in the last 24 hours.",
        "now_some": "Last 24 hours within {r} km: <b>{n}</b> {word}. Closest: {km:.0f} km {dir}.",
        "fires_word": ["fire", "fires", "fires"],
        "risk_line": "14-day forecast for {name}: <b>{lvl}</b> ({p}% chance of above-normal fire activity, usually {u}%).",
        "lvl": {"low": "low", "elevated": "elevated", "high": "high", "very_high": "very high"},
        "alert": "<b>New fire near you</b>\n\nA satellite detected <b>{n}</b> {word} within {r} km. The closest is <b>{km:.0f} km {dir}</b> of your point, {time}.\n{list}\n<a href=\"{map}\">Map</a> · <a href=\"{site}\">FireCalendar</a>\n\n{risk}\n\nIf you see smoke or flames, call <b>112</b>. Do not burn grass or rubbish, keep away from burning steppe and reeds, watch the wind.",
        "alert_item": "• {km:.0f} km {dir}, {time}, power {frp:.0f} MW",
        "time_fmt": "{d}.{m:02d} at {hh:02d}:{mm:02d} ({tz})",
        "demo": "<b>This is a sample (demo)</b> — a real alert looks like this:",
        "stopped": "Unsubscribed. Send your location again to get alerts.",
        "no_sub": "You are not subscribed yet. Send your location ",
        "bad": "I did not get that. Send your location or coordinates like <code>43.24, 76.95</code>.",
        "outside": "That point is outside Central Asia — I only watch Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan and Uzbekistan.",
        "top_risk": "Highest 14-day fire risk (forecast of {d}):",
        "unsub_btn": "Unsubscribe",
        "src": "Data: NASA FIRMS (VIIRS, MODIS). Satellites see heat, not the cause; clouds and smoke can hide fires.",
        "push_title": "New fire near you",
        "push_body": "{n} {word} within {r} km. Closest: {km:.0f} km {dir}, {time}. If you see smoke or flames, call 112.",
    },
}


def word(lang: str, n: int) -> str:
    f = T[lang]["fires_word"]
    if lang == "en":
        return f[0] if n == 1 else f[1]
    a, b = n % 100, n % 10
    return f[2] if 10 < a < 20 else f[0] if b == 1 else f[1] if 1 < b < 5 else f[2]


def local_time(when: dt.datetime, country: str | None, lang: str) -> str:
    off = 6 if country == "Kyrgyzstan" else 5          # Kazakhstan, Uzbekistan, Tajikistan, Turkmenistan: UTC+5
    lt = when + dt.timedelta(hours=off)
    return T[lang]["time_fmt"].format(d=lt.day, m=lt.month, hh=lt.hour, mm=lt.minute, tz=f"UTC+{off}")


def risk_line(static: Static, rid: str | None, lang: str) -> str:
    if not rid or not static.risk or rid not in static.risk["regions"]:
        return ""
    r, name = static.risk["regions"][rid], static.regions[rid].get(lang) or static.regions[rid]["en"]
    return T[lang]["risk_line"].format(name=name, lvl=T[lang]["lvl"][r["level"]], p=round(r["prob"] * 100),
                                       u=round(r["usual_prob"] * 100))


def format_alert(sub: dict, near: list[dict], static: Static) -> str:
    lang, rid = sub["lang"], static.region_at(sub["lat"], sub["lon"])
    country = static.regions.get(rid, {}).get("country") if rid else None
    f0 = near[0]
    items = "\n".join(T[lang]["alert_item"].format(km=f["km"], dir=compass(f["deg"], lang), frp=f["frp"],
                                                   time=local_time(f["when"], country, lang)) for f in near[:3])
    return T[lang]["alert"].format(
        n=len(near), word=word(lang, len(near)), r=sub["radius_km"], km=f0["km"], dir=compass(f0["deg"], lang),
        time=local_time(f0["when"], country, lang), list=items + "\n" if len(near) > 1 else "",
        map=f"https://www.openstreetmap.org/?mlat={f0['lat']:.4f}&mlon={f0['lon']:.4f}#map=11/{f0['lat']:.4f}/{f0['lon']:.4f}",
        site=f"{SITE}/#p={sub['lat']:.3f},{sub['lon']:.3f},{min(200, max(10, int(sub['radius_km'])))}",
        risk=risk_line(static, rid, lang)).replace("\n\n\n", "\n\n")


def format_push(sub: dict, near: list[dict], static: Static) -> dict:
    """Notification for a browser subscriber (title + one-paragraph body + link to the map)."""
    lang = sub.get("lang", "en") if sub.get("lang") in T else "en"
    rid = static.region_at(sub["lat"], sub["lon"])
    country = static.regions.get(rid, {}).get("country") if rid else None
    f0 = near[0]
    body = T[lang]["push_body"].format(n=len(near), word=word(lang, len(near)), r=int(sub["radius_km"]), km=f0["km"],
                                       dir=compass(f0["deg"], lang), time=local_time(f0["when"], country, lang))
    return {"title": T[lang]["push_title"], "body": body, "tag": "fc-alert",
            "url": f"/#p={f0['lat']:.3f},{f0['lon']:.3f},{min(200, max(10, int(sub['radius_km'])))}"}


def web_push():
    """The site's push module (subscriptions in the private Vercel Blob store), or None if not configured."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app import push as wp
        return wp if wp.enabled() else None
    except Exception as e:
        print(f"[push] not available: {e}", flush=True)
        return None


def parse_coords(text: str) -> tuple[float, float] | None:
    import re
    m = re.match(r"^\s*(-?\d{1,2}(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$", text)
    if not m:
        return None
    lat, lon = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def in_central_asia(lat: float, lon: float) -> bool:
    w, s, e, n = map(float, BBOX.split(","))
    return s <= lat <= n and w <= lon <= e


# --------------------------------------------------------------------------- storage
class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS subs (chat_id INTEGER PRIMARY KEY, lat REAL, lon REAL, radius_km REAL,
                                             lang TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS prefs (chat_id INTEGER PRIMARY KEY, lang TEXT);
            CREATE TABLE IF NOT EXISTS sent (chat_id INTEGER, fire_key TEXT, ts REAL, PRIMARY KEY (chat_id, fire_key));
        """)

    def lang(self, chat_id: int) -> str:
        r = self.db.execute("SELECT lang FROM prefs WHERE chat_id=?", (chat_id,)).fetchone()
        return r["lang"] if r else "ru"

    def set_lang(self, chat_id: int, lang: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO prefs VALUES (?, ?)", (chat_id, lang))
        self.db.execute("UPDATE subs SET lang=? WHERE chat_id=?", (lang, chat_id))
        self.db.commit()

    def subscribe(self, chat_id: int, lat: float, lon: float, radius: float | None = None) -> dict:
        old = self.get(chat_id)
        r = radius or (old["radius_km"] if old else DEFAULT_RADIUS)
        self.db.execute("INSERT OR REPLACE INTO subs VALUES (?, ?, ?, ?, ?, ?)",
                        (chat_id, lat, lon, r, self.lang(chat_id), dt.datetime.utcnow().isoformat()))
        self.db.commit()
        return self.get(chat_id)

    def set_radius(self, chat_id: int, r: float) -> dict | None:
        self.db.execute("UPDATE subs SET radius_km=? WHERE chat_id=?", (r, chat_id))
        self.db.commit()
        return self.get(chat_id)

    def get(self, chat_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM subs WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(r) if r else None

    def all(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM subs")]

    def stop(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM subs WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def unseen(self, chat_id: int, fires: list[dict]) -> list[dict]:
        seen = {r["fire_key"] for r in self.db.execute("SELECT fire_key FROM sent WHERE chat_id=?", (chat_id,))}
        return [f for f in fires if f["key"] not in seen]

    def mark(self, chat_id: int, fires: list[dict]) -> None:
        now = time.time()
        self.db.executemany("INSERT OR IGNORE INTO sent VALUES (?, ?, ?)", [(chat_id, f["key"], now) for f in fires])
        self.db.execute("DELETE FROM sent WHERE ts < ?", (now - 3 * 86400,))
        self.db.commit()


# --------------------------------------------------------------------------- telegram
class Bot:
    def __init__(self, token: str, firms_key: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.firms_key = firms_key
        self.store, self.static = Store(), Static()
        self.fires: list[dict] = []
        self.fires_at = 0.0
        self.offset = 0

    def call(self, method: str, **params):
        data = json.dumps(params).encode()
        req = urllib.request.Request(f"{self.base}/{method}", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=CTX, timeout=params.get("timeout", 10) + 15) as r:
            out = json.loads(r.read().decode())
        if not out.get("ok"):
            raise RuntimeError(out.get("description"))
        return out["result"]

    def send(self, chat_id: int, text: str, **kw) -> None:
        try:
            self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True, **kw)
        except Exception as e:
            print(f"[send {chat_id}] {e}", flush=True)
            if "blocked" in str(e) or "chat not found" in str(e):
                self.store.stop(chat_id)                 # user blocked the bot: drop the subscription

    # ---- keyboards
    def loc_kb(self, lang: str) -> dict:
        return {"keyboard": [[{"text": T[lang]["send_loc"], "request_location": True}]], "resize_keyboard": True}

    def radius_kb(self, lang: str, cur: float | None) -> dict:
        row = [{"text": f"{'✓ ' if cur == r else ''}{r} km", "callback_data": f"r:{r}"} for r in RADII]
        return {"inline_keyboard": [row, [{"text": T[lang]["unsub_btn"], "callback_data": "stop"}]]}

    # ---- fires cache
    def current_fires(self, max_age_s: float = 600) -> list[dict]:
        if time.time() - self.fires_at > max_age_s:
            self.fires = fetch_fires(self.firms_key, self.static)
            self.fires_at = time.time()
            print(f"[firms] {len(self.fires)} detections (static sources removed)", flush=True)
        return self.fires

    def status_text(self, sub: dict) -> str:
        lang = sub["lang"]
        near = fires_near(self.current_fires(), sub["lat"], sub["lon"], sub["radius_km"])
        rid = self.static.region_at(sub["lat"], sub["lon"])
        s = (T[lang]["now_some"].format(r=int(sub["radius_km"]), n=len(near), word=word(lang, len(near)), km=near[0]["km"],
                                        dir=compass(near[0]["deg"], lang))
             if near else T[lang]["now_none"].format(r=int(sub["radius_km"])))
        rl = risk_line(self.static, rid, lang)
        return f"{s}\n\n{rl}\n\n<i>{T[lang]['src']}</i>" if rl else f"{s}\n\n<i>{T[lang]['src']}</i>"

    def top_risk(self, lang: str) -> str:
        rk = self.static.risk
        if not rk:
            return T[lang]["bad"]
        top = sorted(rk["regions"].items(), key=lambda kv: -kv[1]["prob"])[:5]
        lines = [f"• {self.static.regions[k].get(lang) or self.static.regions[k]['en']}: {round(r['prob'] * 100)}% "
                 f"({T[lang]['lvl'][r['level']]})" for k, r in top]
        return T[lang]["top_risk"].format(d=rk["issue_date"]) + "\n" + "\n".join(lines)

    # ---- handlers
    def on_location(self, chat_id: int, lat: float, lon: float) -> None:
        lang = self.store.lang(chat_id)
        if not in_central_asia(lat, lon):
            self.send(chat_id, T[lang]["outside"])
            return
        sub = self.store.subscribe(chat_id, lat, lon)
        rid = self.static.region_at(lat, lon)
        region = T[lang]["in_region"].format(name=self.static.regions[rid].get(lang)) if rid else ""
        self.send(chat_id, T[lang]["subscribed"].format(r=int(sub["radius_km"]), lat=lat, lon=lon, region=region,
                                                        m=int(POLL_MINUTES)), reply_markup={"remove_keyboard": True})
        self.send(chat_id, self.status_text(sub))
        self.send(chat_id, T[lang]["radius_q"], reply_markup=self.radius_kb(lang, sub["radius_km"]))
        # fires already burning when you subscribe are reported by /status, not as "new"
        self.store.mark(chat_id, fires_near(self.current_fires(), lat, lon, sub["radius_km"]))

    def on_message(self, m: dict) -> None:
        chat_id = m["chat"]["id"]
        lang = self.store.lang(chat_id)
        if "location" in m:
            self.on_location(chat_id, m["location"]["latitude"], m["location"]["longitude"])
            return
        text = (m.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd == "/start":
            self.send(chat_id, T[lang]["choose_lang"], reply_markup={"inline_keyboard": [[
                {"text": "Қазақша", "callback_data": "lang:kk"}, {"text": "Русский", "callback_data": "lang:ru"},
                {"text": "English", "callback_data": "lang:en"}]]})
        elif cmd == "/help":
            self.send(chat_id, T[lang]["start"], reply_markup=self.loc_kb(lang))
        elif cmd == "/status":
            sub = self.store.get(chat_id)
            self.send(chat_id, self.status_text(sub) if sub else T[lang]["no_sub"], **({} if sub else {"reply_markup": self.loc_kb(lang)}))
        elif cmd == "/radius":
            sub = self.store.get(chat_id)
            self.send(chat_id, T[lang]["radius_q"] if sub else T[lang]["no_sub"],
                      reply_markup=self.radius_kb(lang, sub["radius_km"]) if sub else self.loc_kb(lang))
        elif cmd == "/stop":
            self.store.stop(chat_id)
            self.send(chat_id, T[lang]["stopped"], reply_markup=self.loc_kb(lang))
        elif cmd == "/risk":
            sub = self.store.get(chat_id)
            rl = risk_line(self.static, self.static.region_at(sub["lat"], sub["lon"]), lang) if sub else ""
            self.send(chat_id, (rl + "\n\n" if rl else "") + self.top_risk(lang))
        elif cmd == "/demo":
            self.demo(chat_id)
        elif parse_coords(text):
            self.on_location(chat_id, *parse_coords(text))
        else:
            self.send(chat_id, T[lang]["bad"], reply_markup=self.loc_kb(lang))

    def on_callback(self, q: dict) -> None:
        chat_id = q["message"]["chat"]["id"]
        data = q.get("data", "")
        try:
            self.call("answerCallbackQuery", callback_query_id=q["id"])
        except Exception:
            pass
        if data.startswith("lang:"):
            lang = data[5:] if data[5:] in T else "ru"
            self.store.set_lang(chat_id, lang)
            self.send(chat_id, T[lang]["start"], reply_markup=self.loc_kb(lang))
        elif data.startswith("r:"):
            lang = self.store.lang(chat_id)
            sub = self.store.set_radius(chat_id, float(data[2:]))
            if sub:
                self.store.mark(chat_id, fires_near(self.current_fires(), sub["lat"], sub["lon"], sub["radius_km"]))
                self.send(chat_id, self.status_text(sub), reply_markup=self.radius_kb(lang, sub["radius_km"]))
            else:
                self.send(chat_id, T[lang]["no_sub"], reply_markup=self.loc_kb(lang))
        elif data == "stop":
            lang = self.store.lang(chat_id)
            self.store.stop(chat_id)
            self.send(chat_id, T[lang]["stopped"], reply_markup=self.loc_kb(lang))

    def demo(self, chat_id: int) -> None:
        """Sample alert built from a made-up detection 12 km north-east of the user's point (or Almaty)."""
        lang = self.store.lang(chat_id)
        sub = self.store.get(chat_id) or {"lat": 43.24, "lon": 76.95, "radius_km": DEFAULT_RADIUS, "lang": lang}
        sub = {**sub, "lang": lang}
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40)
        fake = [{"lat": sub["lat"] + 0.077, "lon": sub["lon"] + 0.105, "when": now, "frp": 23.0, "source": "DEMO", "key": "demo"},
                {"lat": sub["lat"] + 0.09, "lon": sub["lon"] + 0.13, "when": now, "frp": 11.0, "source": "DEMO", "key": "demo2"}]
        near = fires_near(fake, sub["lat"], sub["lon"], max(sub["radius_km"], 30))
        self.send(chat_id, T[lang]["demo"] + "\n\n" + format_alert(sub, near, self.static))

    # ---- alert loop
    def check_alerts(self) -> int:
        subs = self.store.all()
        wp = web_push()
        web = []
        if wp:
            try:
                web = wp.store_all()
            except Exception as e:
                print(f"[push] cannot read subscriptions: {e}", flush=True)
        if not subs and not web:
            return 0
        fires = self.current_fires(max_age_s=60)
        sent = pushed = 0
        for sub in subs:
            new = self.store.unseen(sub["chat_id"], fires_near(fires, sub["lat"], sub["lon"], sub["radius_km"]))
            if new:
                self.send(sub["chat_id"], format_alert(sub, new, self.static))
                self.store.mark(sub["chat_id"], new)
                sent += 1
        for ws in web:
            key = f"web:{ws['id']}"
            created = dt.datetime.fromisoformat(ws["created"]) if ws.get("created") else None
            near = [f for f in fires_near(fires, ws["lat"], ws["lon"], ws["radius_km"]) if not created or f["when"] > created]
            new = self.store.unseen(key, near)          # only fires detected after subscribing, each once
            if not new:
                continue
            status = wp.send(ws["sub"], format_push(ws, new, self.static), ttl=6 * 3600)
            if status in (404, 410):                    # browser unsubscribed or data cleared
                wp.store_delete(ws["id"])
                continue
            if status < 300:
                self.store.mark(key, new)
                pushed += 1
        print(f"[alerts] telegram {len(subs)} subscribers / {sent} alerted; browser {len(web)} / {pushed} alerted", flush=True)
        return sent + pushed

    def run(self) -> None:
        me = self.call("getMe")
        print(f"[bot] @{me['username']} running; FIRMS every {POLL_MINUTES:g} min. Ctrl+C to stop.", flush=True)
        self.call("setMyCommands", commands=[
            {"command": "status", "description": "Fires near me now / Қазір жанымда"},
            {"command": "risk", "description": "14-day risk / Қауіп болжамы"},
            {"command": "radius", "description": "Change radius / Радиус"},
            {"command": "stop", "description": "Unsubscribe / Отписаться"},
            {"command": "demo", "description": "Sample alert / Пример"},
            {"command": "help", "description": "Help / Көмек"}])
        next_check = 0.0
        while True:
            try:
                if time.time() >= next_check:
                    try:
                        self.check_alerts()
                    except Exception as e:
                        print(f"[alerts] {e}", flush=True)
                    next_check = time.time() + POLL_MINUTES * 60
                for u in self.call("getUpdates", offset=self.offset, timeout=20, allowed_updates=["message", "callback_query"]):
                    self.offset = u["update_id"] + 1
                    try:
                        if "message" in u:
                            self.on_message(u["message"])
                        elif "callback_query" in u:
                            self.on_callback(u["callback_query"])
                    except Exception as e:
                        print(f"[update] {e}", flush=True)
            except KeyboardInterrupt:
                print("bye")
                return
            except Exception as e:                       # network hiccup: wait and continue
                print(f"[poll] {e}", flush=True)
                time.sleep(5)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test-fetch", action="store_true", help="download FIRMS once and print a summary, no Telegram")
    a = p.parse_args()
    firms = env("FIRMS_MAP_KEY")
    if not firms:
        sys.exit("No FIRMS_MAP_KEY (see scripts/download_recent.py)")
    if a.test_fetch:
        st = Static()
        fires = fetch_fires(firms, st)
        print(f"{len(fires)} detections in the last 24 h after removing static sources")
        for f in sorted(fires, key=lambda f: -f["frp"])[:5]:
            rid = st.region_at(f["lat"], f["lon"])
            print(f"  {f['when']:%Y-%m-%d %H:%M}Z {f['source']:<17} {f['lat']:.3f},{f['lon']:.3f} "
                  f"{f['frp']:6.1f} MW  {st.regions.get(rid, {}).get('en', '-')}")
        return 0
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("No TELEGRAM_BOT_TOKEN. Create a bot with @BotFather and put TELEGRAM_BOT_TOKEN=... in fire-calendar/.env")
    Bot(token, firms).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
