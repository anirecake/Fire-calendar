# Space Apps 2026 project page — texts to paste

Challenge: **Harmonization of MODIS and VIIRS Hot Spots**. Everything below is in English, as the
submission guide requires. Fields marked ✏️ need your input before you submit.

---

## Project Name

**FireCalendar · Central Asia**

## High-Level Summary
*(field limit is short: this is ~120 words)*

FireCalendar turns 26 years of NASA satellite fire detections into one consistent burning-activity
calendar for Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan and Uzbekistan. MODIS (2000→) and the
more sensitive VIIRS (2012→) count fires very differently; we rescale MODIS to VIIRS sensitivity per
0.25° cell and month, so the sensor no longer matters and a 2005 month can be compared with a 2025
month. On top of the harmonized record the web app shows anomalies against the multi-year median,
trends, fire seasons, individual fires with their size and intensity, and an AI 14-day risk forecast
per region. It has views for the public, emergency services and analysts, three languages, CSV and
PNG downloads, an AI assistant and fire alerts near you (browser notifications or Telegram).

## Link to Project Demo ✏️

*Your 7-slide deck or ≤30-second video (public link, no login). Video in Kazakh/Russian is fine with
English subtitles.*

## Link to Final Project

https://fire-calendar.vercel.app — public, no login.
*(✏️ If you add a mirror for judges in Kazakhstan, where *.vercel.app is blocked by some providers,
list it here too.)*

## Detailed Description

### What it does

FireCalendar answers three questions for any area of Central Asia: **is it burning more than usual,
what kind of fires are these, and what comes next?**

1. **Harmonized calendar (the core of the challenge).** A year × month calendar of fire hotspots from
   November 2000 to today for the whole region, a country, any of 48 provinces, a drawn polygon or a
   point with a radius (click the map or type latitude/longitude). Switch between counts and
   anomalies against the multi-year median of the same month.
2. **Analytics for a chosen area and date range:** total with a 90 % uncertainty interval,
   Mann-Kendall trend with Sen's slope, the fire season (shortest run of months holding 80 % of
   fires), this year so far vs the 10th–90th percentile of past years, and "this month so far" vs the
   same days in previous years.
3. **Fire events with properties.** VIIRS detections closer than 1 km and 2 days are linked into
   395,527 individual fires (2012–2026), each with footprint area, duration, peak and total fire
   radiative power, night share, extent, spread speed and recurrence, classified as large wildfire,
   recurring small burn (likely agricultural) or other. Sanity check: the deadly June 2023 fire in
   today's Abai region comes out automatically as one of the three largest fires of 2012–2026
   (324 km², 8 days, 708 MW peak).
4. **AI 14-day risk forecast per province.** A gradient-boosting model with 18 inputs — NASA POWER
   daily weather (max temperature, humidity, vapour-pressure deficit, rain over 1 week to 6 months,
   topsoil and root-zone moisture, mean and max wind, solar radiation), fires already burning and
   their trend, fires in neighbouring provinces, the same weeks last year, and the season — predicts
   whether the next 14 days will be above the province's 80th percentile for that time of year.
   Each forecast explains its drivers and shows similar past situations.
5. **AI forecast down to places (~25 km).** For a provincial emergency service a second model predicts, for every
   ~25 km cell inside a province, the chance of at least one fire in the next 14 days (23 inputs; trained on
   2.4 million cell-weeks 2012–2019, tested on 2020–2025: AUC 0.84; "high" cells → fire 39% of the time vs 6.6%
   on average). Detail follows the selection: Central Asia → provinces, a country → its provinces, a province →
   its places, with a ranked list of the most dangerous places. Two emergency maps: fire danger and recent fires.
6. **AI briefing for each province.** Claude turns the province's facts (risk and drivers, the place forecast,
   weather vs normal, recent fires, history) into a structured briefing: situation, places to watch with
   coordinates, concrete actions (who / what / when) and a ready message for residents — no invented numbers.
7. **Three views for three audiences.** *Everyone* (is it burning more than usual right now?),
   *Emergency* (provinces coloured by risk, why, recommended actions, fires of the last 7 days) and
   *Analyst* (sortable province table, fire explorer, size-vs-intensity plot).
8. **AI assistant.** A chat window where anyone can ask, in Kazakh, Russian or English, "Where is
   the risk highest?" or "What burned near Semey this summer?". The language model (Claude) does not
   invent numbers: it calls our own API (risk model, harmonized statistics, fire list) as tools and
   can move the map to the answer. Without an API key it falls back to offline answers from the
   page's data.
9. **Fire alerts near you — in the browser, any country.** Turn on notifications on the site (phone or
   computer, no app, no account): when NASA FIRMS near-real-time data show a new fire within your radius you
   get a push notification even if the site is closed. Only the point (rounded to ~1 km) and radius are stored.
   **Also in Telegram:** People send their location to our bot and get a message when NASA FIRMS
   near-real-time data show a new fire within their radius: how far, in which direction, when, a map
   link, their province's 14-day risk and safety advice (call 112). Known gas flares and industrial
   heat sources are masked using 26 years of archive labels.
10. **Open data:** monthly calendar CSV, raw hotspots CSV (with the harmonization weight of every
   detection), fire-event CSV, analytics JSON, every chart as PNG, and a documented REST API (`/docs`).

### How it works

* **Data:** NASA FIRMS MODIS Collection 6.1 (Terra + Aqua, 1 km) and VIIRS S-NPP (375 m) archives
  for the five countries; vegetation fires only (FIRMS `type = 0`), MODIS confidence ≥ 30, VIIRS
  nominal/high. The current year comes from the FIRMS API (standard processing, then near-real-time;
  NOAA-20 fills the S-NPP near-real-time outage after 2 Nov 2026).
* **Harmonization:** on a 0.25° grid × calendar month we fit a two-part model on the overlap years
  2012–2021 (before MODIS orbit drift). Where MODIS saw fire: VIIRS ≈ MODIS · r[month] · k[cell],
  with the local multiplier k shrunk toward 1 when data are scarce. Where MODIS saw nothing: an
  expected count of small fires only VIIRS can see. Terra-only months before Aqua (July 2002) are
  first scaled to Terra + Aqua. Every hotspot carries a weight, so any polygon is summed exactly.
* **Validation (out of sample):** trained on 2012–2018, tested on 2019–2021. Region-wide monthly
  totals: R²(log) 0.16 with raw MODIS → 0.97 with a single global ratio → **0.99 with our model**,
  bias +0.8 %. A calibrated two-part error model gives 90 % intervals with 94 % (region), 89 % (1°
  blocks) and 85 % (cells) coverage on the untouched years.
* **Why it matters — headline finding:** the harmonized 2001–2025 record shows that fire activity in
  Central Asia fell by about 60 % per decade (Mann-Kendall p < 0.0001). VIIRS alone (2013+) cannot
  show this (p ≈ 0.10): only the harmonized 25-year series reveals the long-term change.
* **Risk model:** scikit-learn HistGradientBoosting trained on 44,736 province-weeks (2001–2018),
  tested on 2019–2025: AUC 0.814 vs 0.628 for a climatology-only forecast, Brier skill 0.175; when it
  says "high", above-normal fire activity followed 53 % of the time vs 12 % in an ordinary week.
  Permutation importance shows that current fires and season dominate and weather adds less —
  evidence that most fires here are human-caused (stubble, pasture and reed burning), so prevention
  must target burning practices.

### Benefits and intended impact

* **Emergency services and akimats** get a calendar of when and where fires usually happen, a weekly
  risk ranking of provinces with reasons, and a record of past large fires to plan patrols,
  firebreaks and public warnings before the season.
* **Researchers and forestry agencies** get a 25-year consistent fire record (instead of a broken one
  at 2012) with uncertainty, downloadable for any area.
* **People living near the steppe and forests** get plain-language answers and Telegram alerts in
  their own language — Kazakh included.

### Tools, languages, hardware and software

Python (pandas, NumPy, DuckDB, shapely, SciPy, scikit-learn), FastAPI, vanilla JavaScript, Leaflet,
Leaflet.draw, Apache ECharts, Vercel (serverless hosting), Anthropic Claude API (assistant), Telegram
Bot API, Web Push (VAPID) with Vercel Blob storage, pytest (64 automated tests). Runs on an ordinary laptop; no special hardware.

### Creativity and design choices

* A harmonization model that is local (per cell) but safe where data are thin (shrinkage), and
  honest about its limits (calibrated uncertainty, validation on years it never saw).
* Turning raw pixels into **fires with properties**, so users see events, not dots.
* Separate views for different audiences instead of one crowded dashboard, a black "control room"
  design with colour-blind-friendly ramps, and full Kazakh / Russian / English interfaces.
* Using an LLM only as an interface to validated numbers, never as the source of numbers.

### Factors we considered

Sensor differences and orbit drift (MODIS after 2022 is excluded from fitting), static industrial
heat sources (gas flares) in near-real-time data, small samples (percentages are hidden where counts
are tiny), provinces changed by Kazakhstan's 2022 reform (Natural Earth pre-2022 boundaries, noted in
the app), accessibility (colour ramps, keyboard use, readable text), and abuse/cost limits on the AI
assistant.

## Use of Super Intelligence (SI) ✏️ *(check and edit so it is accurate)*

We used **Claude (Anthropic)** as an AI coding assistant throughout the hackathon. Claude wrote most
of the source code under our direction: the data download and processing scripts, the harmonization
and validation code, the risk-model training script, the FastAPI back end, the web front end, the
Telegram bot and the automated tests. It also helped draft explanatory texts and this description.
Before the hackathon we used Claude to compare several challenges; choosing this challenge, the region
(Central Asia), the target audiences and the features (several came from our mentor session) were
our decisions. We tested the app, checked the results (for example that the June 2023 Abai fire is
found) and decided what to change. All data come from NASA and the other sources listed below; no
data were generated by AI. At runtime, the website's chat assistant uses the Claude API to phrase
answers, but every number it gives comes from our own API. The repository README and the app's
"Data & method" tab state this as well.

## NASA Data

* **FIRMS — MODIS Collection 6.1 Active Fire / Hotspots** (Terra and Aqua, 1 km), country archives
  2000–2025 and the FIRMS area API for 2026 — https://firms.modaps.eosdis.nasa.gov/
  (DOI: 10.5067/FIRMS/MODIS/MCD14ML)
* **FIRMS — VIIRS 375 m Active Fire** (Suomi NPP, VNP14IMGTDL_NRT / archive), 2012–2026; NOAA-20 and
  NOAA-21 VIIRS near-real-time for 2026 gap-filling and the Telegram alerts
  (DOI: 10.5067/FIRMS/VIIRS/VNP14IMGT_NRT.002)
* **NASA POWER** (Prediction Of Worldwide Energy Resources) daily meteorology and soil moisture for
  the 48 provinces, 2000–2026 — https://power.larc.nasa.gov/ : T2M_MAX, RH2M, T2MDEW, PRECTOTCORR,
  WS10M, WS10M_MAX, GWETTOP, GWETROOT, ALLSKY_SFC_SW_DWN.

## Space Agency Partner Data

None used.

## Other References

* Natural Earth — Admin 1 states/provinces and Admin 0 countries, 1:10m (public domain)
* Esri — World Light/Dark Gray Canvas basemap tiles (with attribution)
* OpenStreetMap — map links in Telegram alerts
* Giglio, L., Schroeder, W., Justice, C. O. (2016). The collection 6 MODIS active fire detection
  algorithm and fire products. *Remote Sensing of Environment* 178, 31–41.
* Schroeder, W., Oliva, P., Giglio, L., Csiszar, I. A. (2014). The New VIIRS 375 m active fire
  detection data product. *Remote Sensing of Environment* 143, 85–96.
* Mann, H. B. (1945); Kendall, M. G. (1975); Sen, P. K. (1968) — trend test and slope estimator.
* Software: FastAPI, DuckDB, pandas, NumPy, shapely, SciPy, scikit-learn, Leaflet, Leaflet.draw,
  Apache ECharts, Anthropic Python SDK, Telegram Bot API.
