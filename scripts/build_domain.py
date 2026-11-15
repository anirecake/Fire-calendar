"""Site settings of the current domain -> <processed>/domain.json (read by the API and the web page).

Holds what differs between the Central Asia and the United States sites: names, map view, quick places,
emergency number, wording overrides, the context given to the AI, and links between the two sites.
Usage: FC_DOMAIN=us python scripts/build_domain.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import domain  # noqa: E402

SITES = {"ca": "https://fire-calendar.vercel.app", "us": "https://fire-calendar-usa.vercel.app"}

CA = {
    "id": "ca", "bbox": domain.BBOX, "sites": SITES, "emergency_number": "112",
    "name": {"en": "Central Asia", "ru": "Центральная Азия", "kk": "Орталық Азия"},
    "alerts": True, "view": None, "places": None, "i18n": {},
    "ai_region": "Central Asia (Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan, Uzbekistan)",
    "ai_people": "the Ministry of Emergency Situations department of one province and the local akimats",
    "ai_causes": "Most fires here are human-caused (stubble, pasture and reed burning).",
}

US = {
    "id": "us", "bbox": domain.BBOX, "sites": SITES, "emergency_number": "911",
    "name": {"en": "United States", "ru": "США", "kk": "АҚШ"},
    "alerts": False,
    "view": {"south": 24.0, "west": -125.0, "north": 50.0, "east": -66.5},     # contiguous US first; Alaska/Hawaii by panning
    "places": [
        {"lat": 34.15, "lon": -118.3, "r": 75, "en": "Los Angeles", "ru": "Лос-Анджелес", "kk": "Лос-Анджелес"},
        {"lat": 39.5, "lon": -121.8, "r": 100, "en": "Sacramento Valley", "ru": "Долина Сакраменто", "kk": "Сакраменто аңғары"},
        {"lat": 43.6, "lon": -116.2, "r": 100, "en": "Boise, Idaho", "ru": "Бойсе, Айдахо", "kk": "Бойсе, Айдахо"},
        {"lat": 38.4, "lon": -96.5, "r": 100, "en": "Flint Hills, Kansas", "ru": "Флинт-Хилс, Канзас", "kk": "Флинт-Хилс, Канзас"},
        {"lat": 34.8, "lon": -91.0, "r": 100, "en": "Arkansas Delta", "ru": "Дельта Арканзаса", "kk": "Арканзас атырауы"},
        {"lat": 25.9, "lon": -80.9, "r": 75, "en": "Everglades, Florida", "ru": "Эверглейдс, Флорида", "kk": "Эверглейдс, Флорида"},
        {"lat": 33.45, "lon": -112.07, "r": 100, "en": "Phoenix, Arizona", "ru": "Финикс, Аризона", "kk": "Финикс, Аризона"},
        {"lat": 64.84, "lon": -147.7, "r": 150, "en": "Fairbanks, Alaska", "ru": "Фэрбанкс, Аляска", "kk": "Фэрбанкс, Аляска"},
        {"lat": 20.8, "lon": -156.3, "r": 50, "en": "Maui, Hawaii", "ru": "Мауи, Гавайи", "kk": "Мауи, Гавайи"},
    ],
    "i18n": {
        "en": {"region": "United States", "all_ca": "All of the United States", "region_all": "Whole country",
               "countries": {"United States": "United States"}, "field_region": "State", "step_region": "3. State",
               "coord_bad": "Enter decimal degrees inside the United States: latitude {s}…{n}, longitude {w}…{e} (e.g. 34.15 and -118.3).",
               "ai_hello_llm": "Hi! Ask me about fires in the United States: where the risk is high, what burned in your state, how this year compares. I answer only with numbers from this site's data and our risk models, and I can move the map for you.",
               "ai_disclaimer": "AI can make mistakes. In an emergency call 911.",
               "onb_note": "Data: NASA satellites MODIS (since 2000) and VIIRS (since 2012), brought to one scale.",
               "footer": "Data: NASA FIRMS (MODIS C6.1, VIIRS 375 m), NASA POWER. Borders: Natural Earth. Made for NASA Space Apps Challenge 2026."},
        "ru": {"region": "США", "all_ca": "Все США", "region_all": "Вся страна",
               "countries": {"United States": "США"}, "field_region": "Штат", "step_region": "3. Штат",
               "coord_bad": "Введите десятичные градусы в пределах США: широта {s}…{n}, долгота {w}…{e} (например 34.15 и -118.3).",
               "ai_hello_llm": "Привет! Спросите меня о пожарах в США: где высокий риск, что горело в вашем штате, как этот год выглядит на фоне прошлых. Я отвечаю только цифрами из данных этого сайта и наших моделей риска и могу показать нужное на карте.",
               "ai_disclaimer": "ИИ может ошибаться. При пожаре звоните 911."},
        "kk": {"region": "АҚШ", "all_ca": "Бүкіл АҚШ", "region_all": "Бүкіл ел",
               "countries": {"United States": "АҚШ"}, "field_region": "Штат", "step_region": "3. Штат",
               "coord_bad": "АҚШ шегіндегі ондық градусты енгізіңіз: ендік {s}…{n}, бойлық {w}…{e} (мысалы, 34.15 және -118.3).",
               "ai_hello_llm": "Сәлем! АҚШ-тағы өрттер туралы сұраңыз: қай жерде қауіп жоғары, штатыңызда не жанды, биылғы жыл өткен жылдармен салыстырғанда қандай. Мен тек осы сайттың деректері мен біздің қауіп модельдерінің сандарымен жауап беремін және керегін картадан көрсете аламын.",
               "ai_disclaimer": "ЖИ қателесуі мүмкін. Өрт кезінде 911-ге қоңырау шалыңыз."},
    },
    "ai_region": "the United States (50 states and the District of Columbia; contiguous US, Alaska and Hawaii)",
    "ai_people": "a state emergency management agency, the state forestry / fire agency and county fire departments",
    "ai_causes": "Fires here range from large Western wildfires (lightning and human ignitions) to prescribed burns and "
                 "agricultural burning in the Great Plains, the Southeast and the Mississippi Delta.",
}


def main() -> None:
    cfg = US if domain.DOMAIN == "us" else CA
    domain.DATA.mkdir(parents=True, exist_ok=True)
    out = domain.DATA / "domain.json"
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
