"""Admin-1 regions (oblasts, viloyats, velayats) for the five countries:
Natural Earth 1:10m admin-1 (public domain), simplified, written to web/regions.geojson.

Note: Natural Earth still has Kazakhstan's pre-2022 regions (no Abai, Jetisu, Ulytau).

Usage: python scripts/build_regions.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import shapely
from shapely.geometry import mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_firms import CTX, ROOT  # noqa: E402
import domain  # noqa: E402

URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_1_states_provinces.geojson"
CACHE = Path(os.environ.get("TEMP", "/tmp")) / "ne_admin1.geojson"
OUT = domain.WEB_OUT / "regions.geojson"

# fixes / modern names
EN_FIX = {"TJK-368": "Districts of Republican Subordination", "TJK-369": "Sughd", "KAZ-4830": "Astana (city)",
          "KAZ-4829": "Almaty (city)", "UZB-4828": "Tashkent (city)", "KGZ-1115": "Bishkek (city)", "TJK-5491": "Dushanbe (city)"}
RU_FIX = {"KAZ-4830": "Астана (город)", "KAZ-4829": "Алматы (город)", "UZB-4828": "Ташкент (город)",
          "KGZ-1115": "Бишкек (город)", "TJK-5491": "Душанбе (город)", "TJK-369": "Согдийская область",
          "TJK-368": "Районы республиканского подчинения"}
KK = {
    "KAZ-3206": "Шығыс Қазақстан облысы", "KAZ-3207": "Алматы облысы", "KAZ-3252": "Жамбыл облысы",
    "KAZ-3251": "Түркістан облысы", "KAZ-3236": "Маңғыстау облысы", "KAZ-3197": "Қызылорда облысы",
    "KAZ-3195": "Ақтөбе облысы", "KAZ-3204": "Солтүстік Қазақстан облысы", "KAZ-3196": "Қостанай облысы",
    "KAZ-3205": "Павлодар облысы", "KAZ-3201": "Батыс Қазақстан облысы", "KAZ-3198": "Атырау облысы",
    "KAZ-3202": "Ақмола облысы", "KAZ-3203": "Қарағанды облысы", "KAZ-4829": "Алматы қаласы", "KAZ-4830": "Астана қаласы",
}

# Kazakh names for the other four countries' regions
KK.update({
 "UZB-364": "Ферғана облысы",
 "TJK-369": "Соғды облысы",
 "KGZ-1117": "Ыстықкөл облысы",
 "KGZ-1118": "Нарын облысы",
 "KGZ-1122": "Ош облысы",
 "KGZ-1116": "Шу облысы",
 "KGZ-1121": "Талас облысы",
 "KGZ-1120": "Жалалабад облысы",
 "UZB-372": "Ташкент облысы",
 "UZB-365": "Наманган облысы",
 "UZB-363": "Әндіжан облысы",
 "KGZ-1119": "Баткен облысы",
 "TJK-368": "Республикалық бағыныстағы аудандар",
 "TJK-366": "Таулы Бадахшан автономиялық облысы",
 "UZB-371": "Сырдария облысы",
 "UZB-370": "Жизақ облысы",
 "UZB-358": "Самарқанд облысы",
 "UZB-361": "Қашқадария облысы",
 "UZB-362": "Сурхандария облысы",
 "TJK-367": "Хатлон облысы",
 "TKM-359": "Лебап уәлаяты",
 "TKM-360": "Мары уәлаяты",
 "TKM-352": "Ахал уәлаяты",
 "TKM-351": "Балқан уәлаяты",
 "UZB-356": "Қарақалпақстан",
 "UZB-357": "Науаи облысы",
 "TKM-353": "Дашогуз уәлаяты",
 "UZB-355": "Хорезм облысы",
 "UZB-354": "Бұхара облысы",
 "KGZ-1115": "Бішкек (қала)",
 "UZB-4828": "Ташкент (қала)",
 "TJK-5491": "Душанбе (қала)"
})


def main() -> None:
    if CACHE.exists():
        world = json.loads(CACHE.read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(URL, context=CTX, timeout=300) as r:
            world = json.loads(r.read().decode("utf-8"))
    feats = []
    for f in world["features"]:
        p = f["properties"]
        if p.get("admin") not in domain.NE_TO_ADMIN:
            continue
        rid = p["adm1_code"]
        g = shape(f["geometry"]).intersection(shapely.box(*domain.BBOX))      # US: drops Aleutian islands east of 180°
        if g.is_empty:
            continue
        g = shapely.set_precision(g.simplify(0.01, preserve_topology=True), 0.001)
        ru = RU_FIX.get(rid, p.get("name_ru") or p["name"])
        feats.append({"type": "Feature", "geometry": mapping(g), "properties": {
            "id": rid, "country": domain.NE_TO_ADMIN[p["admin"]],
            "en": EN_FIX.get(rid, p.get("name_en") or p["name"]), "ru": ru, "kk": KK.get(rid, ru),
            "city": p.get("type_en") in ("City", "Independent City", "Federal District") or rid in EN_FIX and "(city)" in EN_FIX[rid],
        }})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e3:.0f} kB, {len(feats)} regions)")


if __name__ == "__main__":
    main()
