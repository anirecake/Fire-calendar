/* FireCalendar after-fire reports: list of large fires with a fact sheet, the report dialog (mini map with fire
   detections and NISAR radar layers, growth chart, land cover, settlements, weather, radar check) and the AI
   write-up for journalists and emergency services. Print-friendly (the dialog becomes the printed page). */
(() => {
  "use strict";
  const FC = window.FC;
  if (!FC) return;
  const $ = (id) => document.getElementById(id);
  const t = (k, v) => FC.t(k, v);
  const esc = FC.esc, fmt = FC.fmt;
  const st = { index: null, cur: null, reps: {}, ai: {}, map: null, layer: "points", overlay: null, points: null,
               opacity: 0.8, aud: "j", chart: null };
  const LC_COLOR = { tree: "#2f7d32", shrub: "#8a7d32", grass: "#c9b458", crop: "#e09a3a", built: "#c0392b", bare: "#9e9e9e",
                     snow: "#d8dee4", water: "#3b7dd8", wetland: "#3f9e94", mangrove: "#1f6f5f", moss: "#a7bf8a" };
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const year = (iso) => iso.slice(0, 4);
  const regionLabel = (f) => `${FC.regionName(f.region)}, ${FC.countryName ? FC.countryName(f.country) : f.country}`;
  const placeName = (p) => (p && (p[FC.S.lang] || p.name)) || "";
  const km = (v) => `${fmt(v, v < 10 ? 1 : 0)} ${t("km")}`;

  /* ---------- list ---------- */
  async function index() {
    if (!st.index) {
      try { st.index = await FC.getJSON("/api/fires?limit=200"); } catch (e) { st.index = { fires: [] }; }
    }
    return st.index;
  }
  function dist(lat1, lon1, lat2, lon2) {
    const r = Math.PI / 180, a = Math.sin(((lat2 - lat1) * r) / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
    return 12742 * Math.asin(Math.sqrt(a));
  }
  function inArea(f) {
    const a = FC.S.area;
    if (a.region) return f.region === a.region;
    if (a.country) return f.country === a.country;
    if (a.lat != null && a.radius_km) return dist(a.lat, a.lon, f.lat, f.lon) <= a.radius_km + 20;
    if (a.geometry) {
      const xs = [], ys = [];
      JSON.stringify(a.geometry.coordinates).replace(/\[(-?[\d.]+),(-?[\d.]+)\]/g, (_, x, y) => { xs.push(+x); ys.push(+y); });
      return f.lon >= Math.min(...xs) && f.lon <= Math.max(...xs) && f.lat >= Math.min(...ys) && f.lat <= Math.max(...ys);
    }
    return true;
  }
  function row(f) {
    const near = f.nearest ? ` · ${esc(placeName(f.nearest))} ${esc(km(f.nearest.km))}` : "";
    const cover = f.main_cover ? ` · ${esc(t(`lc_${f.main_cover}`))}` : "";
    return `<button type="button" class="row fr-row" data-fire="${f.id}"><span class="r-main"><i class="t-large"></i><span class="r-txt">
      <b>${esc(regionLabel(f))}</b><small>${esc(FC.dRange(f))}${cover}${near}</small></span></span>
      <span class="r-val">${f.radar ? `<span class="fr-badge">${esc(t("fr_radar_badge"))}</span>` : ""}${fmt(f.area_km2, 0)} km²</span></button>`;
  }
  async function renderList() {
    const box = $("fires-list");
    if (!box) return;
    const ix = await index();
    const list = ix.fires.filter(inArea);
    if (list.length) {
      $("fires-count").textContent = t("fr_count", { n: list.length });
      box.innerHTML = list.slice(0, 40).map(row).join("");
    } else {
      $("fires-count").textContent = t("fr_none_area");
      box.innerHTML = [...ix.fires].sort((a, b) => b.area_km2 - a.area_km2).slice(0, 6).map(row).join("");
    }
  }

  /* ---------- report ---------- */
  async function open(id) {
    id = +id;
    st.cur = id;
    st.layer = "points";
    const dlg = $("fire-dlg");
    dlg.hidden = false;
    document.body.classList.add("fire-open");
    $("fire-body").innerHTML = `<p class="note"><span class="spinner"></span> ${esc(t("loading"))}</p>`;
    try {
      st.reps[id] = st.reps[id] || await FC.getJSON(`/api/fires/${id}`);
    } catch (e) {
      $("fire-body").innerHTML = `<p class="note">${esc(t("error"))}</p>`;
      return;
    }
    paint();
    loadAI(id);
  }
  function close() {
    $("fire-dlg").hidden = true;
    document.body.classList.remove("fire-open");
    if (st.map) { st.map.remove(); st.map = null; }
    if (st.chart) { st.chart.dispose(); st.chart = null; }
    st.cur = null;
  }

  /* the report is built from the main site's own pieces: answer card + facts, folding blocks, ranked rows,
     level badges, the weather grid ("this fire / usual") */
  const CHEV = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;
  function block(id, title, desc, body, extraHead = "") {
    return `<section class="block fr-block" id="frb-${id}"><div class="block-head fold-head"><h2>${esc(title)}</h2>${extraHead}
      <button type="button" class="fold-btn" aria-expanded="true" aria-label="${esc(t("fold_hide"))}" title="${esc(t("fold_hide"))}">${CHEV}</button></div>
      ${desc ? `<p class="desc">${esc(desc)}</p>` : ""}${body}</section>`;
  }
  const fact = (label, value, small = "") => `<div><dt>${esc(label)}</dt><dd>${value}${small ? `<small>${esc(small)}</small>` : ""}</dd></div>`;
  function answerHTML(r) {
    const h = r.history, m = r.modis_burned, near = r.around && r.around.places && r.around.places[0];
    const top = h.rank_region <= 3 ? "lvl-very_high" : h.rank_region <= 10 ? "lvl-high" : "lvl-elevated";
    const burned = m && m.available && !m.missing_months.length ? m.burned_km2 : null;
    const main = esc(t("fr_answer", { d: r.duration_days, y: year(h.since), a: "A", r: "R" }))
      .replace("A", `<b>${esc(fmt(r.footprint_km2, 0))} km²</b>`)
      .replace("R", `<span class="up">${esc(t("fr_answer_rank", { n: h.rank_region }))}</span>`);
    const sub = burned != null ? `<p class="lead">${esc(t("fr_answer_modis", { m: "M" })).replace("M", `<b>${esc(fmt(burned, 0))} km²</b>`)}</p>` : "";
    return `<article class="answer fr-answer">
      <div class="ans-top"><p class="eyebrow">${esc(FC.dRange(r))}</p><span class="lvl ${top}">${esc(t("fr_rank_badge", { n: h.rank_region }))}</span></div>
      <p class="answer-main">${main}</p>${sub}
      <dl class="facts">
        ${fact(t("fr_k_days"), esc(t("fr_days", { n: r.duration_days })), t("fr_k_night_s", { p: fmt(r.night_share * 100) }))}
        ${fact(t("fr_k_det"), fmt(r.detections), t("fr_k_det_s"))}
        ${fact(t("fr_k_frp"), `${fmt(r.frp_max_mw)} MW`, t("fr_k_frp_s"))}
      </dl>
      <dl class="facts">
        ${fact(t("fr_k_area"), `${fmt(r.footprint_km2, 1)} km²`, t("fr_k_area_note"))}
        ${fact(t("fr_k_burned"), burned != null ? `${fmt(burned, 0)} km²` : "–", t("fr_a_modis_n"))}
        ${fact(t("fr_k_nearest"), near ? esc(placeName(near)) : "–", near ? `${km(near.km)}` : t("fr_k_nearest_none"))}
      </dl>
    </article>`;
  }
  function areaHTML(r) {
    const m = r.modis_burned, rd = r.radar, c = r.calibration;
    const card = (label, v, sub, cls = "") => `<div class="risk-card fr-est ${cls}"><div class="rc-name">${esc(label)}</div>
      <div class="rc-prob">${v}</div><div class="rc-sub">${esc(sub)}</div></div>`;
    const modis = m && m.available ? `${fmt(m.burned_km2, 0)}<small> km²${m.missing_months.length ? "*" : ""}</small>` : "–";
    const radar = rd && rd.signal !== "none" ? `${fmt(rd.burned_estimate_km2, 0)}<small> km²</small>` : "–";
    const miss = m && m.available && m.missing_months.length ? `<p class="note">${esc(t("fr_modis_missing", { m: m.missing_months.join(", ") }))}</p>` : "";
    const exp = c ? `<p class="callout">${esc(t("fr_cal_this", { v: fmt(r.footprint_km2 * c.median_ratio, 0), a: fmt(r.footprint_km2 * c.q25, 0), b: fmt(r.footprint_km2 * c.q75, 0) }))}</p>
      <p class="note">${esc(t("fr_cal", { n: c.n_fires, k: fmt(c.median_ratio, 2), a: fmt(c.q25, 2), b: fmt(c.q75, 2) }))}</p>` : "";
    return `<div class="fr-ests">
      ${card(t("fr_a_viirs"), `${fmt(r.footprint_km2, 0)}<small> km²</small>`, t("fr_a_viirs_n"))}
      ${card(t("fr_a_modis"), modis, t("fr_a_modis_n"), "fr-est-main")}
      ${card(t("fr_a_radar"), radar, t(rd ? `fr_a_radar_${rd.signal}` : "fr_a_radar_na"))}
    </div>${miss}${exp}`;
  }
  function coverHTML(lc) {
    if (!lc || !lc.available) return `<p class="note">${esc(t("fr_cover_na"))}</p>`;
    const bar = lc.classes.map((c) => `<span style="flex:${c.share};background:${LC_COLOR[c.class] || "#888"}" title="${esc(t(`lc_${c.class}`))}"></span>`).join("");
    const rows = lc.classes.map((c) => `<li class="row"><span class="r-main"><i style="background:${LC_COLOR[c.class] || "#888"}"></i>
      <span class="r-txt"><b>${esc(t(`lc_${c.class}`))}</b><small>${fmt(c.km2, 1)} km²</small></span></span>
      <span class="r-val">${fmt(c.share * 100)}%</span></li>`).join("");
    return `<div class="fr-cover-bar">${bar}</div><ul class="rows fr-rows">${rows}</ul>`;
  }
  function aroundHTML(a) {
    if (!a) return "";
    if (a.places_ok === false) return `<p class="note">${esc(t("fr_osm_na"))}</p>`;
    const kind = (k) => (k ? t(`place_${k}`) : "");
    const rows = a.places.slice(0, 8).map((p) => `<li class="row"><span class="r-txt"><b>${esc(placeName(p))}</b>
      <small>${esc(kind(p.kind))}${p.population ? ` · ${esc(t("fr_pop_n", { n: fmt(p.population) }))}` : ""}</small></span>
      <span class="r-val">${esc(km(p.km))}</span></li>`).join("");
    const list = a.places.length ? `<ol class="rows rank fr-places">${rows}</ol>` : `<p class="note">${esc(t("fr_places_none", { r: a.radius_km }))}</p>`;
    const pop = a.population_known ? `<p class="note">${esc(t("fr_pop_known", { n: fmt(a.population_known), k: a.population_known_places }))}</p>` : "";
    const roads = a.roads.map((r) => r.ref).filter(Boolean).slice(0, 4).join(", ");
    return `${list}${pop}<dl class="facts">
      ${fact(t("fr_roads"), fmt(a.n_roads), roads)}
      ${fact(t("fr_power"), fmt(a.power_lines))}
      ${fact(t("fr_rail"), fmt(a.railways))}</dl>`;
  }
  function weatherHTML(w) {
    if (!w) return "";
    const cell = (label, now, usual, worse) => `<div><dt>${esc(label)}</dt><dd><span class="${worse ? "up" : ""}">${fmt(now, 1)}</span>
      <small>${usual == null ? "&nbsp;" : esc(t("fr_usual", { v: fmt(usual, 1) }))}</small></dd></div>`;
    return `<dl class="weather-grid">
      ${cell(t("fr_w_tmax"), w.tmax, w.tmax_usual, w.tmax > (w.tmax_usual ?? Infinity))}
      ${cell(t("fr_w_rh"), w.rh_min, w.rh_usual, w.rh_min < (w.rh_usual ?? -Infinity))}
      ${cell(t("fr_w_wind"), w.wind_max, w.wind_usual, w.wind_max > (w.wind_usual ?? Infinity))}
      ${cell(t("fr_w_rain30"), w.rain_30d_before_mm, w.rain_30d_before_usual_mm, w.rain_30d_before_mm < (w.rain_30d_before_usual_mm ?? -Infinity))}
      ${cell(t("fr_w_rain"), w.rain_during_mm, null, false)}
      <div><dt>${esc(t("fr_w_src"))}</dt><dd><small>NASA POWER</small></dd></div>
    </dl>`;
  }
  function radarHTML(r, id) {
    if (!r) return `<p class="callout">${esc(t("fr_radar_na"))}</p>`;
    const pdf = r.reports.includes(`report-${FC.S.lang}.pdf`) ? `report-${FC.S.lang}.pdf` : r.reports[0];
    const lvl = { clear: "lvl-high", weak: "lvl-elevated", none: "lvl-low" }[r.signal];
    return `<div class="ans-top"><span class="lvl ${lvl}">${esc(t(`fr_sig_${r.signal}`))}</span></div>
      <p class="callout">${esc(t(`fr_radar_${r.signal}`))}</p>
      <dl class="facts">
        ${fact(t("fr_r_dates"), `${esc(r.before.slice(5).split("-").reverse().join("."))} → ${esc(r.after.slice(5).split("-").reverse().join("."))}`, `${(r.channel || "").replace(/(..)\1/, "$1")} · ${fmt(r.grid_m)} m · ${r.maturity || ""}`)}
        ${fact(t("fr_r_inside"), `${fmt(r.median_inside_db, 1)} / ${fmt(r.median_outside_db, 1)} dB`)}
        ${fact(t("fr_r_drop", { t: fmt(r.threshold_db, 1) }), `${fmt(r.drop_share_inside * 100)}% / ${fmt(r.drop_share_outside * 100)}%`)}
      </dl>${pdf ? `<a class="btn fr-pdf" href="/api/fires/${id}/files/${pdf}" target="_blank" rel="noopener">${esc(t("fr_r_pdf"))}</a>` : ""}`;
  }
  function paint() {
    const id = st.cur, r = st.reps[id];
    if (!r) return;
    const layers = ["points"].concat(r.radar ? ["delta", "before", "after"] : []);
    const seg = `<div class="seg fr-layers" role="tablist">${layers.map((l) => `<button type="button" role="tab" data-layer="${l}" aria-selected="${st.layer === l}">${esc(t(`fr_layer_${l}`))}</button>`).join("")}</div>`;
    $("fire-body").innerHTML = `
      <header class="fr-head">
        <div class="fr-head-top">
          <p class="eyebrow">${esc(t("fr_eyebrow"))}</p>
          <div class="fr-tools">
            <button type="button" class="btn" id="fr-map-btn"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14"/></svg><span>${esc(t("fr_on_map"))}</span></button>
            <button type="button" class="btn" id="fr-print"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9V3h12v6M6 18H4v-7h16v7h-2M7 14h10v7H7z"/></svg><span>${esc(t("fr_print"))}</span></button>
            <button type="button" class="icon-btn" id="fr-close" aria-label="${esc(t("ai_close"))}" title="${esc(t("ai_close"))}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
          </div>
        </div>
        <h1 id="fr-title">${esc(regionLabel(r))}</h1>
        <p class="view-hint">${esc(t("fr_head_hint", { lat: fmt(r.lat, 3), lon: fmt(r.lon, 3) }))}</p>
      </header>
      <div class="fr-scroll">
        ${answerHTML(r)}
        ${block("map", t("fr_map"), "", `<div id="fr-map" class="fr-map"></div>
          <div class="fr-legend"><p class="note" id="fr-legend"></p>${r.radar ? `<label class="toggle fr-op"><span>${esc(t("fr_opacity"))}</span><input type="range" id="fr-opacity" min="0.2" max="1" step="0.05" value="${st.opacity}"></label>` : ""}</div>`, seg)}
        <section class="block fr-block" id="frb-ai"><div id="fr-ai" class="fr-ai"></div></section>
        ${block("area", t("fr_area"), t("fr_area_desc"), areaHTML(r))}
        ${block("growth", t("fr_growth"), t("fr_growth_desc"), `<div id="fr-growth" class="chart fr-chart"></div>`)}
        ${block("cover", t("fr_cover"), t("fr_cover_desc"), coverHTML(r.land_cover))}
        ${block("around", t("fr_around"), t("fr_around_desc", { r: r.around ? r.around.radius_km : 15 }), aroundHTML(r.around))}
        ${block("weather", t("fr_weather"), t("fr_weather_desc"), weatherHTML(r.weather))}
        ${block("radar", t("fr_radar"), t("fr_radar_desc"), radarHTML(r.radar, id))}
        <section class="block fr-block"><p class="note fr-src">${esc(t("fr_sources", { d: st.index && st.index.built ? FC.dateFmt(st.index.built) : "" }))}</p></section>
      </div>`;
    $("fr-close").onclick = close;
    $("fr-print").onclick = () => window.print();
    $("fr-map-btn").onclick = () => {
      const b = r.bbox;
      close();
      if (FC.map) FC.map.flyToBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [40, 40], maxZoom: 11, duration: 0.6 });
    };
    document.querySelectorAll(".fr-layers button").forEach((b) => { b.onclick = () => setLayer(b.dataset.layer); });
    document.querySelectorAll("#fire-body .fr-block .fold-btn").forEach((b) => {
      b.onclick = () => {
        const sec = b.closest(".block"), folded = !sec.classList.contains("folded");
        sec.classList.toggle("folded", folded);
        b.setAttribute("aria-expanded", String(!folded));
        b.title = t(folded ? "fold_show" : "fold_hide"); b.setAttribute("aria-label", b.title);
        if (!folded) { if (st.chart) st.chart.resize(); if (st.map) st.map.invalidateSize(); }
      };
    });
    const op = $("fr-opacity");
    if (op) op.oninput = () => { st.opacity = +op.value; if (st.overlay) st.overlay.setOpacity(st.opacity); };
    drawMap(r);
    drawGrowth(r);
    paintAI();
  }

  /* ---------- mini map ---------- */
  function dayColor(d, n) {
    const ramp = ["#ffe066", "#ffb000", "#ff7a1a", "#e8401c", "#a8170f", "#5c0b07"];
    const x = n <= 1 ? 0 : d / (n - 1);
    return ramp[Math.min(ramp.length - 1, Math.floor(x * (ramp.length - 1) + 0.0001))];
  }
  function drawMap(r) {
    if (st.map) { st.map.remove(); st.map = null; }
    const m = L.map("fr-map", { zoomControl: true, attributionControl: true, preferCanvas: true });
    const dark = FC.S.theme === "dark";
    const base = dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base";
    L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${base}/MapServer/tile/{z}/{y}/{x}`,
      { attribution: "Tiles &copy; Esri", maxZoom: 16 }).addTo(m);
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 17, opacity: 0.55, attribution: "Imagery &copy; Esri" }).addTo(m);
    const n = r.duration_days;
    st.points = L.layerGroup(r.points.map((p) => L.circleMarker([p[0], p[1]], { radius: 3, stroke: false, fillColor: dayColor(p[2], n), fillOpacity: 0.9 }))).addTo(m);
    const b = r.bbox;
    m.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [16, 16], maxZoom: 12 });
    st.map = m;
    setLayer(st.layer, true);
    setTimeout(() => m.invalidateSize(), 60);
  }
  function setLayer(l, quiet) {
    const r = st.reps[st.cur];
    if (!r || !st.map) return;
    st.layer = l;
    document.querySelectorAll(".fr-layers button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.layer === l)));
    if (st.overlay) { st.map.removeLayer(st.overlay); st.overlay = null; }
    if (l !== "points" && r.radar) {
      const [w, s, e, n] = r.radar.bounds;
      st.overlay = L.imageOverlay(`/api/fires/${st.cur}/files/${l}.png`, [[s, w], [n, e]], { opacity: st.opacity }).addTo(st.map);
      st.points.eachLayer((c) => c.setStyle({ fillOpacity: 0.55, radius: 2 }));
      if (!quiet) st.map.fitBounds([[s, w], [n, e]], { padding: [8, 8] });
    } else {
      st.points.eachLayer((c) => c.setStyle({ fillOpacity: 0.9, radius: 3 }));
    }
    st.points.bringToFront && st.points.eachLayer((c) => c.bringToFront());
    $("fr-legend").textContent = l === "points" ? t("fr_points_legend") : l === "delta" ? t("fr_delta_legend", { r: fmt(r.radar.range_db, 1) }) : t("fr_img_legend");
  }

  /* ---------- growth chart ---------- */
  function drawGrowth(r) {
    if (st.chart) st.chart.dispose();
    const el = $("fr-growth");
    if (!el || !window.echarts) return;
    const ch = echarts.init(el);
    const days = r.growth.map((g) => g.date.slice(5).split("-").reverse().join("."));
    const text = css("--text-2"), line = css("--line");
    ch.setOption({
      animation: false, grid: { left: 44, right: 44, top: 28, bottom: 28 },
      tooltip: { trigger: "axis" },
      legend: { top: 0, right: 0, textStyle: { color: text }, itemWidth: 12, itemHeight: 8 },
      xAxis: { type: "category", data: days, axisLabel: { color: text }, axisLine: { lineStyle: { color: line } } },
      yAxis: [{ type: "value", name: "km²", nameTextStyle: { color: text }, axisLabel: { color: text }, splitLine: { lineStyle: { color: line } } },
              { type: "value", axisLabel: { color: text }, splitLine: { show: false } }],
      series: [{ name: t("fr_new"), type: "bar", data: r.growth.map((g) => g.new_km2), itemStyle: { color: css("--accent") }, barMaxWidth: 26 },
               { name: t("fr_total"), type: "line", yAxisIndex: 1, data: r.growth.map((g) => g.total_km2), symbol: "none",
                 lineStyle: { color: css("--accent-2"), width: 2 }, itemStyle: { color: css("--accent-2") } }],
    });
    st.chart = ch;
  }

  /* ---------- AI write-up ---------- */
  async function loadAI(id) {
    const k = `${id}|${FC.S.lang}`;
    if (st.ai[k] && st.ai[k] !== "error") { paintAI(); return; }
    st.ai[k] = "loading";
    paintAI();
    try {
      st.ai[k] = await FC.getJSON(`/api/ai/fire-report?id=${id}&lang=${FC.S.lang}`);
    } catch (e) {
      st.ai[k] = "error";
    }
    if (st.cur === id) paintAI();
  }
  function paintAI() {
    const box = $("fr-ai");
    if (!box) return;
    const a = st.ai[`${st.cur}|${FC.S.lang}`];
    const head = `<div class="ai-head"><span class="ai-tag">${esc(t("ai_tag"))}</span><span class="ai-title">${esc(t("fr_ai"))}</span></div>`;
    if (!a || a === "loading") { box.innerHTML = `<div class="ai-card ai-loading">${head}<p class="note"><span class="spinner"></span> ${esc(t("fr_ai_loading"))}</p></div>`; return; }
    if (a === "error") { box.innerHTML = `<div class="ai-card">${head}<p class="note">${esc(t("fr_ai_err"))}</p></div>`; return; }
    const nums = a.key_numbers.map((n) => `<div><dt>${esc(n.label)}</dt><dd>${esc(n.value)}</dd></div>`).join("");
    const p = (title, txt) => (txt ? `<h4>${esc(title)}</h4><p>${esc(txt)}</p>` : "");
    const j = `<div class="fr-aud" data-aud="j"><p class="lead fr-headline">${esc(a.headline)}</p><p>${esc(a.summary)}</p>
      <dl class="fr-nums">${nums}</dl>${p(t("fr_why"), a.why)}${p(t("fr_impact"), a.impact)}${p(t("fr_radar_ai"), a.radar)}
      <button type="button" class="btn fr-copy">${esc(t("fr_copy"))}</button></div>`;
    const e = a.for_emergency;
    const acts = e.actions.map((x, i) => `<li><span class="ai-num">${i + 1}</span><div><b>${esc(x.what)}</b>
      <div class="aa-meta"><span><small>${esc(t("ai_who"))}</small>${esc(x.who)}</span><span><small>${esc(t("ai_when"))}</small>${esc(x.when)}</span></div></div></li>`).join("");
    const em = `<div class="fr-aud" data-aud="e">${p(t("fr_assess"), e.assessment)}
      <h4>${esc(t("fr_lessons"))}</h4><ul class="fr-lessons">${e.lessons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <h4>${esc(t("fr_actions"))}</h4><ol class="fr-actions">${acts}</ol></div>`;
    box.innerHTML = `<div class="ai-card">${head}
      <div class="seg fr-auds" role="tablist"><button type="button" role="tab" data-a="j" aria-selected="${st.aud === "j"}">${esc(t("fr_ai_j"))}</button>
        <button type="button" role="tab" data-a="e" aria-selected="${st.aud === "e"}">${esc(t("fr_ai_e"))}</button></div>
      <div class="fr-aud-wrap" data-show="${st.aud}">${j}${em}</div>
      <h4>${esc(t("fr_caveats"))}</h4><ul class="fr-caveats">${a.caveats.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <p class="note ai-note">${esc(t("fr_ai_note"))}</p></div>`;
    box.querySelectorAll(".fr-auds button").forEach((b) => {
      b.onclick = () => { st.aud = b.dataset.a; paintAI(); };
    });
    const cp = box.querySelector(".fr-copy");
    if (cp) cp.onclick = async () => {
      const txt = [a.headline, a.summary, ...a.key_numbers.map((n) => `${n.label}: ${n.value}`), a.why, a.impact, a.radar].filter(Boolean).join("\n\n");
      try { await navigator.clipboard.writeText(txt); cp.textContent = t("ai_copied"); } catch (err) { /* clipboard blocked */ }
    };
  }

  /* ---------- wiring ---------- */
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-fire]");
    if (b) { e.preventDefault(); open(b.dataset.fire); return; }
    if (e.target.id === "fire-dlg") close();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && st.cur != null) close(); });
  window.addEventListener("resize", () => { if (st.chart) st.chart.resize(); if (st.map) st.map.invalidateSize(); });
  FC.fireLink = (id) => (st.index && st.index.fires.some((f) => f.id === id)
    ? `<br><button type="button" class="link-btn" data-fire="${id}">${esc(t("fr_ev_link"))}</button>` : "");
  document.addEventListener("fc:render", renderList);
  document.addEventListener("fc:lang", () => { renderList(); if (st.cur != null) { paint(); loadAI(st.cur); } });
  document.addEventListener("fc:theme", () => { if (st.cur != null) paint(); });
  index().then(renderList);
  const deep = /[#&]fire=(\d+)/.exec(location.hash);
  if (deep) document.addEventListener("fc:ready", () => open(deep[1]), { once: true });
})();
