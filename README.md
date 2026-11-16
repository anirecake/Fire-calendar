# FireCalendar · Central Asia

NASA Space Apps Challenge 2026 — *Harmonization of MODIS and VIIRS Hot Spots*.

**Live demo: https://fire-calendar.vercel.app**

A web app that merges two decades of MODIS active-fire hotspots with the newer
VIIRS record into one consistent series, and shows it as a **burning-activity
calendar** for any area of interest in Kazakhstan, Uzbekistan, Kyrgyzstan,
Tajikistan and Turkmenistan.

## What it does

- **Harmonizes** MODIS (2000→) to VIIRS (2012→) sensitivity, so the sensor of origin no longer matters.
- **Calendar** (years × months) of harmonized hotspots, with an **anomaly vs multi-year median** mode.
- **Any area**: pick a country or draw a rectangle/polygon on the map.
- **Why harmonize** chart: raw MODIS vs raw VIIRS vs harmonized annual totals.
- **Seasonal profile** of any year vs the median.
- **Downloads**: calendar CSV and raw hotspots CSV for the area and dates.
- **Validation** table: out-of-sample skill of the harmonization model.

## Method

1. FIRMS archive hotspots, presumed vegetation fires only (`type = 0`);
   MODIS confidence ≥ 30, VIIRS confidence nominal/high.
2. 0.25° grid × calendar month.
3. On overlap years (2012–2021, before MODIS orbit drift) fit
   `VIIRS ≈ MODIS · r[month] · k[cell]`:
   `r` = regional seasonal ratio, `k` = local multiplier shrunk toward 1
   (pseudo-count prior) so data-poor cells follow the regional ratio.
   Terra-only months (before Aqua, Jul 2002) are first scaled to Terra+Aqua.
4. Harmonized series = VIIRS from 2012-01-20, weighted MODIS before.
   Every hotspot carries its weight `w`, so any polygon is summed exactly.
5. Validation: fit on 2012–2018, predict VIIRS from MODIS for 2019–2021,
   compared with no scaling and with a single global ratio.

## Beyond the calendar: what kind of fires, why, and what next

**Three views for three audiences** (switch at the top of the left panel):

| View | For | Map | Report |
|---|---|---|---|
| Everyone | public, press, judges | smooth heat field | "is it burning more than usual right now?" |
| Emergency | fire services, akimats | regions coloured by 14-day risk + fires of the last 7 days | risk, why, weather vs normal, past analogs, what to do |
| Analyst | researchers, forestry | regions by a chosen number (fires/yr, trend, last month, large fires, risk) | sortable region table, fire-event explorer, size-vs-intensity plot, CSV |

**Fire events with properties** (`scripts/build_events.py`): VIIRS detections ≤1 km and ≤2 days
apart are linked into one fire → 395,527 fires (2012–2026), each with area (active-fire footprint),
duration, peak/summed FRP, night share, extent, spread speed, recurrence, and a type
(large wildfire / recurring small burn, likely agricultural / small fire).
Sanity check: the deadly 8 June 2023 fire in today's Abai region (then East Kazakhstan) comes out
as the #3 largest event of 2012–2026 (324 km², 8 days, peak 708 MW) — found automatically.

**AI forecast: 14-day risk per region** (`scripts/build_weather.py`, `scripts/build_risk.py`):
gradient boosting with 18 inputs — NASA POWER daily weather for 48 admin-1 regions (max
temperature, humidity, vapour-pressure deficit, rain over 7/30/90/180 days, topsoil and root-zone
moisture, mean and max wind, solar radiation), fires already burning and their 7-day trend, fires
in neighbouring regions, the same weeks last year, and the season — predicts whether the next 14
days will be above the region's 80th percentile.
Hindcast 2019–2025 (never seen in training): AUC 0.814 vs 0.628 for climatology, Brier skill 0.175
(first 9-input model: 0.161); when it says "high", above-normal fires followed 53 % of the time vs
12 % in an ordinary week. Permutation importance: current fires ≫ season > dryness > heat >
neighbours > fuel (3–6-month rain) > last year > wind. Finding: most fires here are man-made,
so weather explains little and prevention must target burning practices.

**AI forecast for places: ~25 km cells inside a province** (`scripts/build_risk_cells.py`): for a provincial
emergency service the province number is too coarse, so a second gradient-boosting model predicts, for every
0.25° cell with past VIIRS activity, the chance that satellites detect at least one fire there in the next 14 days.
Inputs (23): fires in the cell (14 d, 8 weeks), the same 14 days last year, the place's usual season and activity,
fires in the 8 surrounding cells, the province's NASA POWER weather anomalies and fire activity, week of year.
Trained on 2.4 M cell-weeks (2012–2019), tested on 1.9 M (2020–2025): AUC 0.841 (place climatology 0.835),
Brier skill +0.06; when it says "high" (≥25%) a fire followed 39% of the time vs 6.6% on average, and 57% of
all fires fell in "high" cells. Most of the skill is knowing where each place usually burns; current fires and
weather add a modest gain. Levels of detail in the Emergency view: whole Central Asia → provinces; a country →
its provinces with %; **one province → its ~25 km places**, plus a ranked list "most dangerous places".

**Two emergency maps**: *Fire danger* (risk; places when a province is selected) and *Recent fires* (fires of the
last 7 days by type), switched in the legend.

**AI briefing for a province** (`app/briefing.py`, `GET /api/ai/briefing?region=&lang=`): instead of generic
advice, Claude receives the province's facts only — 14-day risk and drivers, the place-level forecast (top
cells with coordinates and part of the province), weather vs normal, fires of the last 14 days, fire history —
and returns structured JSON: headline, situation, places to watch, 4–6 concrete actions (who / what / when) and
a message for residents. Cached per province, language and forecast date (memory + private Vercel Blob).

**AI assistant** (`app/assistant.py`, `web/assistant.js`): a chat window (chat button, bottom right) where anyone
can ask in Kazakh, Russian or English. Claude (Anthropic API, `claude-opus-5-5`, low effort, server-side
refusal fallback enabled) answers by calling our own functions as tools — `get_risk_forecast`,
`get_area_summary`, `find_fires`, `show_on_map` — so every number comes from the harmonized data and
the risk model above, and the answer can move the map. Needs `ANTHROPIC_API_KEY` (Vercel env var or
`.env`); without it the widget answers the quick questions offline from the page's data. Rate limited
(15 questions / 10 min per visitor, 400 / hour per instance).

**Telegram fire alerts** (`bot/firebot.py`): send your location, pick a radius (10–100 km); every
30 min the bot downloads NASA FIRMS near-real-time detections (VIIRS NOAA-20/21/S-NPP + MODIS, last
24 h), masks 1,792 known static heat sources (gas flares, plants; `scripts/build_static_mask.py`) and
messages each subscriber about new fires inside their radius: distance, direction, local time, map
link, the province's 14-day risk and safety advice. `/status`, `/risk`, `/radius`, `/stop`, `/demo`.
Run it on any computer with internet: `python bot/firebot.py` (needs `TELEGRAM_BOT_TOKEN` from
@BotFather; set `TELEGRAM_BOT_USERNAME` on the web server to show the "Telegram alerts" button).

**Browser fire alerts (Web Push, any country, no app)** (`app/push.py`, `web/push.js`, `web/sw.js`): in the
"Fire alerts near you" block, pick your location (or the centre of the selected area) and a radius and turn
alerts on. The browser subscribes with our VAPID key; the subscription (point rounded to ~1 km, radius,
language) is stored in a **private Vercel Blob** store, and a welcome push is sent at once. The same alert loop
as the Telegram bot (`bot/firebot.py`) sends a notification for every new FIRMS fire inside the radius, even
when the site is closed (Android, Windows, macOS, Linux; iPhone after "Add to Home Screen"). Only fires
detected after subscribing are sent, each once; expired subscriptions are deleted. Env: `VAPID_PUBLIC_KEY`,
`VAPID_PRIVATE_KEY`, `BLOB_READ_WRITE_TOKEN`.

Also: type latitude/longitude for point search, and every chart has a ↓ PNG button.

### After the fire: reports for journalists and emergency services (with SARWatch / NISAR)

Tab **After fire** (every view). `scripts/build_fire_reports.py` builds a fact sheet for the largest fires
(the biggest since 2025 plus the biggest on record), served by `app/fires.py` and shown by `web/fires.js`:

- **Footprint and growth**: the fire's own VIIRS 375 m detections (same linking rule as the events), day by day.
- **Burned area, one unit from three satellites**: VIIRS active-fire footprint; **NASA MODIS MCD64A1** burned-area
  map (500 m, read from Microsoft Planetary Computer) next to the fire; **NASA NISAR** L-band radar before/after
  comparison computed with our partner team's **SARWatch** service. Over all fires a calibration gives
  "km² burned per km² of VIIRS footprint", so every fire, also the ones before MCD64A1 months are published, can
  be stated in burned km² with a likely range.
- **What burned**: ESA WorldCover 2021 (10 m) land types inside the fire area.
- **Who and what is nearby**: settlements within 15 km with distance and population, main roads, power lines,
  railways crossing the fire area (OpenStreetMap, Overpass API).
- **Weather during the fire** vs the same days in earlier years (NASA POWER), and the fire's **size rank** in its
  province since 2012.
- **Radar check (NISAR)**: median radar change inside the fire area vs pixels with no fire detection between the
  two passes, share of pixels with a clear drop, an experimental burned-area estimate, the radar images on the
  map and SARWatch's PDF report. Honest result so far: with passes 6–8 weeks apart the radar does not separate
  the burned area on Kazakh grassland and only weakly in US forests; the report says so.
- **AI write-up** (`GET /api/ai/fire-report?id=&lang=`): Claude turns the fact sheet into a news-style summary for
  journalists and an after-action note (assessment, lessons, who/what/when actions) for emergency services, in
  Kazakh, Russian or English, using only the numbers and names in the fact sheet (structured JSON output, cached).
- **Print / PDF** button: the report becomes a printable page.

```bash
python scripts/build_fire_reports.py --nisar <SARWatch results>   # then:
python scripts/build_fire_reports.py --modis-only                 # MODIS burned area + calibration
```

## API

Interactive docs: `/docs`. Every POST takes the same area body (all optional):
`{"country": "Kazakhstan"}`, `{"geometry": <GeoJSON Polygon>}` or
`{"lat": 50.4, "lon": 80.2, "radius_km": 25}`, plus `"start"` / `"end"` (YYYY-MM-DD).

| Endpoint | What |
|---|---|
| `GET /api/meta` | coverage dates, model coefficients, validation, uncertainty |
| `POST /api/calendar` | year × month harmonized counts, 90 % interval, source per month, anomalies, month-to-date |
| `POST /api/analytics` | totals for the period (± 90 %), Mann-Kendall trend + Sen slope (harmonized and VIIRS-only), fire season, year-to-date vs p10/p50/p90 |
| `POST /api/calendar.csv` | monthly series as CSV |
| `POST /api/hotspots.csv` | raw filtered hotspots (≤ 100k rows) |
| `GET /api/grid?year=&month=` | per-cell harmonized counts for the map |
| `GET /api/regions` | 48 admin-1 regions: fires/yr, trend, last month, recent events, risk |
| `POST /api/events` | fire events with properties: filter by type/area/date, sort, summary by year |
| `GET /api/risk/cells?region=` | place-level (~25 km) 14-day fire probability inside a province |
| `GET /api/ai/briefing?region=&lang=` | AI briefing: situation, places to watch, concrete actions, message for residents |
| `GET /api/risk` | 14-day risk per region: probability, drivers, weather vs normal, analogs, validation |
| `POST /api/assistant` | AI assistant: `{"messages":[{"role":"user","content":"..."}],"lang":"ru"}` → reply, map actions, tools used |
| `GET /api/assistant/status` | is the language-model assistant configured |
| `GET /api/links` | optional Telegram bot username |
| `GET /api/push/config` · `POST /api/push/subscribe` · `/unsubscribe` · `/test` | browser push alerts |
| `GET /api/grid?region=…` / `country=` / `bbox=w,s,e,n` | map cells for the selected area only |
| `GET /api/fires?region=&country=&bbox=` | large fires with an after-fire report |
| `GET /api/fires/{id}` · `/api/fires/{id}/files/{name}` | fact sheet (growth, burned area, land cover, settlements, weather, NISAR radar) and radar images / PDF |
| `GET /api/ai/fire-report?id=&lang=` | AI after-fire report for journalists and emergency services |
| `GET /api/health` | liveness + cache stats |

Uncertainty: rescaled-MODIS months carry a 90 % interval from a two-part error model
(local heavy-tailed part, calibrated, + regional systematic part). Parameters are estimated
on 2012–2015 → 2016–2018 and checked on untouched 2019–2021: coverage 94 % (region),
89 % (1° blocks), 85 % (cells).

Headline finding: harmonized 2001–2025 Central Asia shows a significant decline
(≈ −60 %/decade, Mann-Kendall p < 0.0001); VIIRS alone (2013+) cannot show it (p ≈ 0.10).

Tests: `python -m pytest tests` (75 tests: API, statistics, data invariants, events, risk model, AI assistant with a fake Claude client, Telegram bot logic, browser push alerts).

## Run locally

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt # Windows
python scripts/download_firms.py      # ~200 yearly CSVs from NASA FIRMS
python scripts/download_recent.py     # current year via FIRMS API (needs FIRMS_MAP_KEY in .env)
python scripts/build_regions.py       # admin-1 regions (Natural Earth)
python scripts/build_dataset.py       # filter, grid, harmonize, validate, assign regions
python scripts/build_events.py        # hotspots -> fire events with properties
python scripts/build_weather.py       # NASA POWER daily weather per region
python scripts/build_risk.py          # 14-day risk model + hindcast
python scripts/build_risk_cells.py    # place-level (~25 km) risk model + hindcast
python scripts/build_static_mask.py   # gas flares / plants mask for live Telegram alerts
uvicorn app.main:app --port 8000      # open http://localhost:8000
```

Deploy (Vercel, FastAPI serverless; `api/index.py` is the entry point, only
`data/processed` is uploaded):

```bash
vercel deploy --prod
```

Windows note: if `pyarrow` fails with "file name too long", the project path is
too deep — create the venv at a short path (e.g. `%USERPROFILE%\.venvs\firecal`).

## Key numbers (Central Asia, 2000-11 → 2025-12)

- 1.20 M MODIS + 1.64 M VIIRS vegetation-fire hotspots after filtering.
- VIIRS/MODIS ratio ≈ 3.4 and stable in 2012–2021; ≈ 4.1 in 2022–2025 (MODIS orbit drift).
- Out-of-sample (2019–2021), whole region monthly: R²(log) 0.16 raw → 0.97 single ratio → **0.99 ours**, bias +0.8 %.
- ~12 % of VIIRS detections fall in cell-months where MODIS saw nothing — handled by the small-fire term.

## Current year (2026)

- From the FIRMS API: Standard Processing to Jul/Aug, Near Real-Time after.
- NRT has no `type`: country from the archive (99.6 % correct in a 2025 back-test), static sources
  masked with archive locations; monthly VIIRS counts within ~5 % of the archive in the back-test.
- Suomi NPP NRT stops on 2026-11-02; 3–13 Nov filled with NOAA-20 VIIRS × 0.958
  (S-NPP / NOAA-20 ratio over the previous 60 days).
- "This month so far" KPI compares month-to-date with the same days of the month in 2000–2025.

## Data

NASA FIRMS — MODIS Collection 6.1 and VIIRS S-NPP 375 m active fire archives,
https://firms.modaps.eosdis.nasa.gov/ · NASA POWER daily weather · NASA MODIS MCD64A1 v6.1 burned area (via
Microsoft Planetary Computer) · NASA NISAR L-band GCOV (via SARWatch, ASF) · ESA WorldCover 2021 ·
OpenStreetMap (ODbL) · Natural Earth borders.

## Use of AI (Space Apps "Use of Super Intelligence" disclosure)

The code in this repository was written with the help of Claude (Anthropic), an AI coding assistant,
under the team's direction; the team chose the challenge, region, audiences and features, and tested
the results. All data come from NASA FIRMS, NASA POWER and Natural Earth; no data were generated by
AI. The website's chat assistant uses the Claude API at runtime to phrase answers, with all numbers
taken from this app's own API.

## Two sites from one code base: Central Asia and the United States

`scripts/domain.py` selects the area with `FC_DOMAIN` (`ca` default, `us`). The same scripts build both:

```bash
export FC_DOMAIN=us                                   # PowerShell: $env:FC_DOMAIN = "us"
python scripts/download_firms.py                      # US archives, stored as .csv.gz
python scripts/download_recent.py                     # current year via the FIRMS area API
python scripts/build_borders.py && python scripts/build_regions.py   # 50 states + DC (Natural Earth)
python scripts/build_domain.py                        # names, map view, quick places, 911, AI context
python scripts/build_dataset.py && python scripts/build_events.py
python scripts/build_weather.py && python scripts/build_risk.py
FC_CELL_MIN=10 FC_CELL_NEG=0.25 python scripts/build_risk_cells.py   # big domain: sampled negatives, corrected
python scripts/stage_us.py                            # ../fire-calendar-usa = same app + US data
cd ../fire-calendar-usa && vercel deploy --prod       # separate Vercel project
```

The page reads `domain.json` through `/api/meta` (names, map view, quick places, emergency number, wording) and
shows a "Central Asia | USA" switcher linking the two sites. Browser push alerts and the Telegram bot are
Central Asia only for now.
