/* FireCalendar front end — full-screen map, country → region picker, three audience views with tabs.
   Vanilla JS + Leaflet + ECharts. */
(() => {
  "use strict";

  /* ---------------- constants ---------------- */
  const RAMPS = {   // heat colours (inferno on dark, yellow → red on light)
    dark: ["#2a0f57", "#4a0c6b", "#781c6d", "#a52c60", "#cf4446", "#ed6925", "#fb9b06", "#f7d13d", "#fcffa4"],
    light: ["#fde68a", "#fcd34d", "#fdba4d", "#fb9a3c", "#f97730", "#ea5a24", "#cf3e1d", "#a92a19", "#7c1d14"],
  };
  const DIVERGE = ["#1f5f80", "#4f8aa6", "#8fb6c8", "#cddde3", "#ece6dc", "#f0c39f", "#e88a5a", "#d0502a", "#9c1c0c"];
  const TILES = { light: ["World_Light_Gray_Base", "World_Light_Gray_Reference"], dark: ["World_Dark_Gray_Base", "World_Dark_Gray_Reference"] };
  const TYPE_COLOR = { large: "#ff3b1f", recurrent: "#ffb000", small: "#8c857b" };
  const LVL = ["low", "elevated", "high", "very_high"];
  const TABS = {
    overview: ["now", "season", "trend", "after", "data"],
    emergency: ["now", "why", "act", "after", "regions"],
    analyst: ["regions", "fires", "after", "props", "data"],
  };
  const SMALL = 5;   // typical count below which percentages are noise
  const $ = (id) => document.getElementById(id);
  const isPhone = () => window.matchMedia("(max-width: 760px)").matches;
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } } };

  const S = {
    lang: detectLang(), theme: document.documentElement.dataset.theme || "dark",
    view: ["overview", "emergency", "analyst"].includes(store.get("fc-view")) ? store.get("fc-view") : "overview",
    tab: { overview: "now", emergency: "now", analyst: "regions" },
    mode: "country", area: {}, labelFn: null, from: null, to: null,
    cal: null, an: null, dataKey: null, meta: null, regions: [], regionsById: {}, risk: null,
    active: null, activeSince: null, events: null, evScatter: null,
    metric: "avg", calMode: "count", showRaw: false,
    evTypes: new Set(["large", "recurrent", "small"]), evMin: 0, evSort: "area_km2",
    regSort: { key: "avg", asc: false }, open: { risk: false, active: false, reg: false, ev: 15 },
    emMode: "risk", riskView: "cells", cells: null, brief: {},
    point: null, place: null, radius: 50, mapYear: "", mapMonth: null, playing: false, grid: null, seq: 0, slowTimer: null,
  };
  let C = {};

  /* ---------------- i18n helpers ---------------- */
  function detectLang() {
    const s = store.get("fc-lang");
    if (["en", "ru", "kk"].includes(s)) return s;
    const n = (navigator.language || "").toLowerCase();
    if (n.startsWith("kk")) return "kk";
    return /^(ru|ky|uz|tg|be|uk)/.test(n) ? "ru" : "en";
  }
  const D = () => I18N[S.lang];
  const t = (k, v = {}) => {
    const s = D()[k] ?? I18N.en[k] ?? k;
    return typeof s === "string" ? s.replace(/\{(\w+)\}/g, (_, x) => (v[x] ?? "")) : s;
  };
  const locale = () => (S.lang === "en" ? "en-US" : "ru-RU");      // kk numbers follow the ru pattern
  const fmt = (v, d = 0) => (v == null || Number.isNaN(+v) ? "–" : Number(v).toLocaleString(locale(), { maximumFractionDigits: d }));
  const plural = (n, f) => {
    n = Math.abs(Math.round(n));
    if (f.length === 2) return n === 1 ? f[0] : f[1];
    const a = n % 100, b = n % 10;
    if (a > 10 && a < 20) return f[2];
    return b === 1 ? f[0] : b > 1 && b < 5 ? f[1] : f[2];
  };
  const hs = (n) => `${fmt(n)} ${plural(n, D().hotspots_n)}`;
  const M = (i) => D().months[i];
  const MF = (i) => D().months_full[i];
  const MG = (i) => D().months_gen[i];
  const MI = (i) => D().months_in[i];
  const countryName = (c) => D().countries[c] || c;
  const regionName = (id) => (S.regionsById[id] ? S.regionsById[id][S.lang] : id);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const signed = (p) => `${p > 0 ? "+" : p < 0 ? "−" : ""}${fmt(Math.abs(p))}`;
  const pfmt = (p) => (p < 0.001 ? "< 0.001" : fmt(p, 3));
  const pct = (p) => `${fmt(p * 100)}%`;
  const median = (a) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const dateFmt = (iso) => { const [y, m, d] = iso.split("-").map(Number); return t("date_fmt", { y, d, m: MG(m - 1) }); };
  const dRange = (e) => (e.start === e.end ? dateFmt(e.start) : `${dateFmt(e.start)} – ${dateFmt(e.end)}`);

  function compare(anom, v, med, lo, hi) {
    if (med != null && med < SMALL && lo != null) {
      if (v > hi) return { text: t("above_range"), cls: "up" };
      if (v < lo) return { text: t("below_range"), cls: "down" };
      return { text: t("within_range"), cls: "" };
    }
    if (anom == null || Math.abs(anom) < 10) return { text: t("same"), cls: "" };
    return anom > 0 ? { text: t("more", { p: fmt(anom) }), cls: "up" } : { text: t("less", { p: fmt(-anom) }), cls: "down" };
  }
  function vsNormal(p, med) {
    if (med != null && med < SMALL) return `<span>${t("typical_small", { m: fmt(med) })}</span>`;
    if (p == null) return "–";
    return p >= 0 ? `<span class="${p >= 10 ? "up" : ""}">${t("vs_normal_more", { p: fmt(p) })}</span>`
      : `<span class="${p <= -10 ? "down" : ""}">${t("vs_normal_less", { p: fmt(-p) })}</span>`;
  }

  /* ---------------- theme ---------------- */
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    C = { accent: v("--accent"), accent2: v("--accent-2"), muted: v("--muted"), line: v("--line"), line2: v("--line-2"),
      text: v("--text"), text2: v("--text-2"), panel: v("--panel"), panel2: v("--panel-2"), band: v("--band"),
      up: v("--up"), down: v("--down"), ok: v("--ok"), ramp: RAMPS[S.theme],
      modis: S.theme === "dark" ? "#8fb7cc" : "#2c6a88", viirs: S.theme === "dark" ? "#ffd27a" : "#9a6400",
      mono: "IBM Plex Mono, monospace", body: "Golos Text, sans-serif" };
    C.lvl = Object.fromEntries(LVL.map((l) => [l, v(`--lvl-${l}`)]));
  }

  /* ---------------- map ---------------- */
  const map = L.map("map", { preferCanvas: true, worldCopyJump: true, zoomSnap: 0.5, zoomControl: false }).setView([45, 66], 4);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const tileLayers = [];
  function setTiles() {
    tileLayers.forEach((l) => map.removeLayer(l));
    tileLayers.length = 0;
    const url = (n) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${n}/MapServer/tile/{z}/{y}/{x}`;
    const [base, ref] = TILES[S.theme];
    tileLayers.push(L.tileLayer(url(base), { attribution: "Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap · Borders: Natural Earth", maxZoom: 16 }).addTo(map));
    tileLayers.push(L.tileLayer(url(ref), { maxZoom: 16, pane: "shadowPane", opacity: 0.8 }).addTo(map));
  }
  const areaLayer = L.featureGroup().addTo(map);
  const eventsLayer = L.featureGroup().addTo(map);
  const labelsLayer = L.layerGroup().addTo(map);
  const cellsLayer = L.featureGroup().addTo(map);
  let heatLayer = null, bordersLayer = null, regionsLayer = null;
  const gridCache = new Map();
  let gridIndex = new Map(), gridCell = 0.25, gridSeq = 0;
  const tip = $("map-tip");
  const showTip = (e, html) => { tip.hidden = false; tip.innerHTML = html; tip.style.left = `${e.originalEvent.clientX}px`; tip.style.top = `${e.originalEvent.clientY}px`; };

  const rampT = (v) => Math.max(0, Math.min(1, Math.log10(Math.max(v, 1)) / 3));
  const rampColor = (v) => C.ramp[Math.min(C.ramp.length - 1, Math.round(rampT(v) * (C.ramp.length - 1)))];

  /* only the selected area is requested: province / country by id, drawn shape or point by its bounding box */
  function gridArea(q) {
    const a = S.area;
    if (a.region) q.set("region", a.region);
    else if (a.country) q.set("country", a.country);
    else if ((a.geometry || a.lat != null) && areaLayer.getLayers().length) {
      const b = areaLayer.getBounds();
      q.set("bbox", [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((x) => x.toFixed(2)).join(","));
    }
  }
  function getGrid(year, month) {
    const q = new URLSearchParams();
    if (year) q.set("year", year);
    if (month) q.set("month", month);
    gridArea(q);
    const k = q.toString();
    if (!gridCache.has(k)) gridCache.set(k, getJSON(`/api/grid?${k}`).catch((e) => { gridCache.delete(k); throw e; }));
    return gridCache.get(k);
  }
  /* smooth heat: grid drawn sharp on a scratch canvas in Mercator rows, blurred ONCE, shown as an image */
  function drawHeat(g) {
    const d = g.cell, cells = g.cells.filter((c) => c[2] >= 0.5);
    if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
    if (!cells.length) return;
    let s = 90, n = -90, w = 180, e = -180;
    for (const [lat, lon] of cells) { s = Math.min(s, lat); n = Math.max(n, lat + d); w = Math.min(w, lon); e = Math.max(e, lon + d); }
    const pad = 1.5; s -= pad; n += pad; w -= pad; e += pad;
    const px = 5, yM = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
    const W = Math.round(((e - w) / d) * px), top = yM(n), bot = yM(s);
    const H = Math.round((W * (top - bot)) / (((e - w) * Math.PI) / 180));
    const raw = document.createElement("canvas"); raw.width = W; raw.height = H;
    const rc = raw.getContext("2d");
    for (const [lat, lon, v] of cells) {
      const x0 = ((lon - w) / d) * px, y0 = ((top - yM(lat + d)) / (top - bot)) * H, y1 = ((top - yM(lat)) / (top - bot)) * H;
      rc.globalAlpha = 0.5 + 0.5 * rampT(v);
      rc.fillStyle = rampColor(v);
      rc.fillRect(x0, y0, px + 0.6, y1 - y0 + 0.6);
    }
    const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
    const ctx = cv.getContext("2d");
    if ("filter" in ctx) ctx.filter = `blur(${px * 0.85}px)`;
    ctx.drawImage(raw, 0, 0);
    heatLayer = L.imageOverlay(cv.toDataURL(), [[s, w], [n, e]], { opacity: 0.95, className: "heat", interactive: false });
    if (S.view === "overview") { heatLayer.addTo(map); heatLayer.bringToBack(); }
  }
  async function showGrid() {
    const y = S.mapMonth ? S.mapMonth.y : S.mapYear || null, m = S.mapMonth ? S.mapMonth.m : null;
    let g;
    const tok = ++gridSeq;                       // a newer area/year request wins over a slower older one
    try { g = await getGrid(y, m); } catch (e) { return; }
    if (tok !== gridSeq) return;
    S.grid = g; gridCell = g.cell; gridIndex = new Map();
    if (S.cal && S.an && S.view !== "analyst") renderAnswer();
    for (const [lat, lon, v] of g.cells) if (v >= 0.5) gridIndex.set(`${Math.round(lat / gridCell)}|${Math.round(lon / gridCell)}`, v);
    drawHeat(g);
    renderLegend();
  }
  map.on("mousemove", (e) => {
    if (S.view !== "overview") return;
    const v = gridIndex.get(`${Math.floor(e.latlng.lat / gridCell)}|${Math.floor(e.latlng.lng / gridCell)}`);
    if (!v) { tip.hidden = true; return; }
    showTip(e, esc(S.mapYear || S.mapMonth ? t("tip_one", { v: fmt(v) }) : t("tip_avg", { v: fmt(v) })));
  });
  map.on("mouseout", () => { tip.hidden = true; });

  /* ---------------- countries + regions on the map ---------------- */
  const selCountry = () => S.area.country || (S.area.region && S.regionsById[S.area.region] ? S.regionsById[S.area.region].country : null);
  function borderStyle(f) {
    const sel = selCountry() === f.properties.name;
    const clickable = S.view === "overview" && !selCountry();
    return { color: sel ? C.accent : C.text, weight: sel ? 2 : 0.8, opacity: sel ? 0.95 : 0.35, fill: clickable, fillOpacity: 0, dashArray: sel ? null : "2 3" };
  }
  function metricValue(r, m) {
    if (!r) return null;
    switch (m) {
      case "avg": return r.avg_per_year;
      case "trend": return r.trend_pct_decade;
      case "last": return r.last_month ? r.last_month.anomaly_pct : null;
      case "large": return r.events_recent ? r.events_recent.large : 0;
      case "risk": { const k = S.risk && S.risk.regions[r.id]; return k ? k.prob : null; }
      default: return null;
    }
  }
  const metricFmt = (v, m) => (v == null ? "–" : m === "trend" || m === "last" ? `${signed(v)}%` : m === "risk" ? pct(v) : fmt(v));
  function metricColor(v, m) {
    if (v == null) return null;
    if (m === "risk") return C.lvl[LVL[v >= 0.6 ? 3 : v >= 0.4 ? 2 : v >= 0.2 ? 1 : 0]];
    if (m === "trend" || m === "last") {
      const x = Math.max(-1, Math.min(1, m === "trend" ? v / 80 : Math.sign(v) * Math.log2(1 + Math.abs(v) / 100) / 2));
      return DIVERGE[Math.round(((x + 1) / 2) * (DIVERGE.length - 1))];
    }
    const max = Math.log10(Math.max(...S.regions.map((r) => metricValue(r, m) || 0), 1) + 1);
    return C.ramp[Math.min(C.ramp.length - 1, Math.round((Math.log10(v + 1) / max) * (C.ramp.length - 1)))];
  }
  const viewMetric = () => (S.view === "emergency" ? "risk" : S.metric);
  function regionStyle(f) {
    const p = f.properties, sel = S.area.region === p.id;
    if (S.view === "overview") {
      const inC = selCountry() === p.country;
      return { color: C.text, weight: sel ? 0 : inC ? 0.7 : 0, opacity: inC ? 0.6 : 0, fill: inC, fillOpacity: 0, fillColor: C.text };
    }
    if (S.view === "emergency" && S.emMode === "fires") return { color: C.line2, weight: 0.7, opacity: 1, fill: true, fillColor: C.panel2, fillOpacity: 0.25 };
    const v = metricValue(S.regionsById[p.id], viewMetric());
    const col = metricColor(v, viewMetric());
    if ((sel || p.country === S.area.country) && S.view === "emergency" && cellsShown()) return { color: C.panel, weight: 0.7, opacity: 1, fill: true, fillColor: col || C.panel2, fillOpacity: 0.12 };
    return { color: C.panel, weight: 0.7, opacity: 1, fill: true, fillColor: col || C.panel2, fillOpacity: col ? (sel ? 0.9 : 0.72) : 0.3 };
  }
  function regionTip(id) {
    const m = viewMetric(), r = S.regionsById[id];
    let s = `<b>${esc(regionName(id))}</b>`;
    if (S.view !== "overview") s += `<br><span class="num">${esc(metricFmt(metricValue(r, m), m))}</span> · ${esc(t(`metric_${m}`))}`;
    const k = S.risk && S.risk.regions[id];
    if (m === "risk" && k) s += ` · ${esc(t(`lvl_${k.level}`))}`;
    return s;
  }
  async function loadGeo() {
    const [countries, regions] = await Promise.all([getJSON("/static/countries.geojson?v=40"), getJSON("/static/regions.geojson?v=40")]);
    regionsLayer = L.geoJSON(regions, {
      style: regionStyle,
      onEachFeature: (f, layer) => {
        layer.on("click", () => { if (!mapPicking()) selectRegion(f.properties.id, { fit: false }); });
        layer.on("mouseover", (e) => { if (S.view !== "overview" || selCountry() === f.properties.country) { layer.setStyle({ weight: 2.5 }); showTip(e, regionTip(f.properties.id)); } });
        layer.on("mousemove", (e) => { if (!tip.hidden) showTip(e, regionTip(f.properties.id)); });
        layer.on("mouseout", () => { regionsLayer.resetStyle(layer); tip.hidden = true; });
      },
    }).addTo(map);
    bordersLayer = L.geoJSON(countries, {
      style: borderStyle, interactive: true,
      onEachFeature: (f, layer) => layer.on("click", () => { if (!mapPicking() && S.view === "overview" && !selCountry()) selectCountry(f.properties.name); }),
    }).addTo(map);
  }
  /* spotlight: everything outside the selected area is dimmed and the area gets a white (dark theme) or black
     outline with a contrasting halo, so the selection reads on any background (heat field, risk colours) */
  map.createPane("spot");
  map.getPane("spot").style.zIndex = 450;
  map.getPane("spot").style.pointerEvents = "none";
  const spotRenderer = L.svg({ pane: "spot", padding: 0.5 });
  const spotLayer = L.layerGroup().addTo(map);
  const WORLD = [[-89, -540], [89, -540], [89, 540], [-89, 540]];
  const outerRings = (g) => (g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [])
    .map((p) => p[0].map(([x, y]) => [y, x]));
  function selectedGeometry() {
    const a = S.area;
    let g = null;
    if (a.region && regionsLayer) regionsLayer.eachLayer((l) => { if (l.feature.properties.id === a.region) g = l.feature.geometry; });
    else if (a.country && bordersLayer) bordersLayer.eachLayer((l) => { if (l.feature.properties.name === a.country) g = l.feature.geometry; });
    else if (a.geometry) g = a.geometry;
    else if (a.lat != null && a.radius_km) {
      const dLat = a.radius_km / 111.32, dLon = a.radius_km / (111.32 * Math.max(Math.cos((a.lat * Math.PI) / 180), 0.01));
      const ring = Array.from({ length: 73 }, (_, i) => { const t2 = (i / 72) * 2 * Math.PI; return [a.lon + dLon * Math.cos(t2), a.lat + dLat * Math.sin(t2)]; });
      g = { type: "Polygon", coordinates: [ring] };
    }
    return g;
  }
  function drawSpotlight() {
    spotLayer.clearLayers();
    const g = selectedGeometry();
    const rings = g ? outerRings(g) : [];
    if (!rings.length) return;
    const dark = S.theme === "dark", choropleth = S.view !== "overview";
    L.polygon([WORLD, ...rings], { renderer: spotRenderer, interactive: false, stroke: false,
      fillColor: dark ? "#000" : "#2b2520", fillOpacity: dark ? (choropleth ? 0.42 : 0.5) : (choropleth ? 0.3 : 0.36) }).addTo(spotLayer);
    for (const r of rings) {
      L.polygon(r, { renderer: spotRenderer, interactive: false, color: dark ? "#000" : "#fff", weight: 7, opacity: 0.8,
        fill: true, fillColor: dark ? "#fff" : "#000", fillOpacity: dark ? 0.12 : 0.06 }).addTo(spotLayer);
      L.polygon(r, { renderer: spotRenderer, interactive: false, fill: false, color: dark ? "#fff" : "#111", weight: 2.5, opacity: 1 }).addTo(spotLayer);
    }
  }
  function restyleMap() {
    drawSpotlight();
    if (regionsLayer) regionsLayer.setStyle(regionStyle);
    if (bordersLayer) bordersLayer.setStyle(borderStyle);
    // click priority: overview without a country -> countries; otherwise regions on top
    if (regionsLayer && bordersLayer) {
      if (S.view === "overview" && !selCountry()) bordersLayer.bringToFront();
      else { bordersLayer.bringToBack(); regionsLayer.bringToFront(); }
    }
    drawLabels();
  }
  function drawLabels() {
    labelsLayer.clearLayers();
    if (!regionsLayer || S.view === "overview" || (S.view === "emergency" && (S.emMode === "fires" || cellsShown()))) return;
    const m = viewMetric(), c = selCountry();
    regionsLayer.eachLayer((l) => {
      const p = l.feature.properties, r = S.regionsById[p.id];
      const v = metricValue(r, m);
      if (v == null || p.city) return;
      const show = c ? p.country === c : m === "risk" ? v >= 0.2 : false;
      if (!show) return;
      L.marker(l.getBounds().getCenter(), { interactive: false, keyboard: false,
        icon: L.divIcon({ className: "rlabel-icon", html: `<span>${esc(metricFmt(v, m))}</span>`, iconSize: [0, 0] }) }).addTo(labelsLayer);
    });
  }
  /* ---------------- place-level risk (~25 km cells) inside the selected province ---------------- */
  const cellsKey = () => (S.area.region ? S.area.region : S.area.country ? `c:${S.area.country}` : null);
  const cellsShown = () => S.view === "emergency" && S.emMode === "risk" && !!S.cells && !!cellsKey() && S.cells.key === cellsKey();
  const cellLevel = (p) => (p >= 0.5 ? "very_high" : p >= 0.25 ? "high" : p >= 0.1 ? "elevated" : "low");
  function partOf(lat, lon) {
    let b = null;
    if (regionsLayer) regionsLayer.eachLayer((l) => { if (l.feature.properties.id === S.area.region) b = l.getBounds(); });
    if (!b) return "";
    const fy = (lat - b.getSouth()) / (b.getNorth() - b.getSouth() || 1), fx = (lon - b.getWest()) / (b.getEast() - b.getWest() || 1);
    const ns = fy > 0.62 ? "n" : fy < 0.38 ? "s" : "", ew = fx > 0.62 ? "e" : fx < 0.38 ? "w" : "";
    return t(`part_${ns + ew || "c"}`);
  }
  function cellTip(c) {
    const drv = c.drivers.length ? `<br>${c.drivers.map((d) => esc(t(`cdrv_${d}`))).join(" · ")}` : "";
    return `<b>${pct(c.prob)}</b> · ${esc(t(`lvl_${cellLevel(c.prob)}`))}<br>${esc(t("cell_tip"))}<br><span class="num">${esc(t("cell_tip_fires", { n: fmt(c.fires_last14), y: fmt(c.fires_same14_last_year) }))}</span>${drv}`;
  }
  /* smooth look: the cell probabilities painted on a Mercator canvas and blurred once, like the public heat map */
  const lvlColor = (p) => C.lvl[cellLevel(p)];
  function drawRiskSmooth() {
    const d = S.cells.cell_deg, cells = S.cells.cells;
    let s = 90, n = -90, w = 180, e = -180;
    for (const c of cells) { s = Math.min(s, c.lat); n = Math.max(n, c.lat + d); w = Math.min(w, c.lon); e = Math.max(e, c.lon + d); }
    const pad = 0.75; s -= pad; n += pad; w -= pad; e += pad;
    const px = 8, yM = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
    const W = Math.round(((e - w) / d) * px), top = yM(n), bot = yM(s);
    const H = Math.max(1, Math.round((W * (top - bot)) / (((e - w) * Math.PI) / 180)));
    const raw = document.createElement("canvas"); raw.width = W; raw.height = H;
    const rc = raw.getContext("2d");
    for (const c of cells) {
      const x0 = ((c.lon - w) / d) * px, y0 = ((top - yM(c.lat + d)) / (top - bot)) * H, y1 = ((top - yM(c.lat)) / (top - bot)) * H;
      rc.globalAlpha = Math.min(1, 0.25 + c.prob * 1.6);
      rc.fillStyle = lvlColor(c.prob);
      rc.fillRect(x0, y0, px + 0.6, y1 - y0 + 0.6);
    }
    const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
    const ctx = cv.getContext("2d");
    if ("filter" in ctx) ctx.filter = `blur(${px * 0.9}px)`;
    ctx.drawImage(raw, 0, 0);
    L.imageOverlay(cv.toDataURL(), [[s, w], [n, e]], { opacity: 0.9, interactive: false }).addTo(cellsLayer);
  }
  function nearestCell(latlng) {
    if (!cellsShown()) return null;
    const d = S.cells.cell_deg;
    return S.cells.cells.find((c) => latlng.lat >= c.lat && latlng.lat < c.lat + d && latlng.lng >= c.lon && latlng.lng < c.lon + d) || null;
  }
  map.on("mousemove", (e) => {
    if (S.view !== "emergency" || S.riskView !== "smooth") return;
    const c = nearestCell(e.latlng);
    if (c) showTip(e, cellTip(c)); else if (cellsShown()) tip.hidden = true;
  });
  function drawCells() {
    cellsLayer.clearLayers();
    $("risk-views").hidden = !cellsShown();
    document.querySelectorAll("#risk-views button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.rv === S.riskView)));
    if (!cellsShown()) return;
    if (S.riskView === "smooth") { drawRiskSmooth(); return; }
    const d = S.cells.cell_deg;
    for (const c of S.cells.cells) {
      const lvl = cellLevel(c.prob);
      L.rectangle([[c.lat, c.lon], [c.lat + d, c.lon + d]], { color: C.panel, weight: 0.6, fillColor: C.lvl[lvl],
        fillOpacity: lvl === "low" ? 0.25 + c.prob * 2 : 0.82 })
        .on("mouseover", (e) => showTip(e, cellTip(c))).on("mousemove", (e) => showTip(e, cellTip(c))).on("mouseout", () => { tip.hidden = true; })
        .on("click", (e) => { showTip(e, cellTip(c)); })
        .addTo(cellsLayer);
    }
    cellsLayer.bringToFront();
  }
  function setEmMode(m) {
    S.emMode = m;
    document.querySelectorAll("#em-modes button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.em === m)));
    restyleMap(); drawCells(); renderLegend();
    if (S.active) drawEvents(m === "fires" ? S.active.events.slice(0, 300) : []);
  }
  document.querySelectorAll("#em-modes button").forEach((b) => { b.onclick = () => setEmMode(b.dataset.em); });
  document.querySelectorAll("#risk-views button").forEach((b) => { b.onclick = () => { S.riskView = b.dataset.rv; tip.hidden = true; drawCells(); }; });
  document.querySelectorAll("#em-modes button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.em === S.emMode)));

  function fitPadded(bounds) {
    const top = document.querySelector(".picker").getBoundingClientRect().bottom + 10;
    const opts = isPhone()
      ? { paddingTopLeft: [10, top], paddingBottomRight: [10, $("sheet").offsetHeight + 60] }
      : { paddingTopLeft: [360, 80], paddingBottomRight: [document.body.classList.contains("sheet-hidden") ? 20 : 510, 70] };
    map.flyToBounds(bounds, { ...opts, duration: 0.6, maxZoom: 8 });
  }

  /* ---------------- area selection: country → region, or draw / point ---------------- */
  function fillRegionSelect() {
    const single = S.meta && S.meta.countries.length === 1;
    document.body.classList.toggle("single-country", !!single);
    const c = $("country").value || (single ? S.meta.countries[0] : ""), sel = $("region");
    if (!c) { sel.innerHTML = `<option value="">${esc(t("region_first"))}</option>`; sel.disabled = true; return; }
    const list = S.regions.filter((r) => r.country === c).sort((a, b) => regionName(a.id).localeCompare(regionName(b.id)));
    sel.innerHTML = `<option value="">${esc(t("region_all"))}</option>` + list.map((r) => `<option value="${esc(r.id)}">${esc(regionName(r.id))}</option>`).join("");
    sel.disabled = false;
    sel.value = S.area.region && S.regionsById[S.area.region] && S.regionsById[S.area.region].country === c ? S.area.region : "";
  }
  function setArea(area, labelFn, { layer = null, hash = null } = {}) {
    S.area = area; S.labelFn = labelFn;
    areaLayer.clearLayers();
    if (layer) areaLayer.addLayer(layer);
    if (hash !== null) history.replaceState(null, "", hash || location.pathname + location.search);
    S.open = { risk: false, active: false, reg: false, ev: 15 };
    restyleMap();
    showGrid();
    load();
  }
  function selectCountry(c, { fit = true } = {}) {
    stopDraw(); setPointMode(false); S.mode = "country";
    $("country").value = c || "";
    setArea(c ? { country: c } : {}, () => (c ? countryName(c) : t("all_ca")), { hash: c ? `#c=${encodeURIComponent(c)}` : "" });
    fillRegionSelect();
    if (!fit) return;
    if (c && bordersLayer) bordersLayer.eachLayer((l) => { if (l.feature.properties.name === c) fitPadded(l.getBounds()); });
    else if (S.meta) { const b = (S.domain && S.domain.view) || S.meta.bounds; fitPadded(L.latLngBounds([b.south, b.west], [b.north, b.east])); }
  }
  function selectRegion(id, { fit = true } = {}) {
    if (!S.regionsById[id]) return;
    if (isPhone()) { foldPicker(true); document.body.classList.remove("sheet-min"); }
    stopDraw(); setPointMode(false); S.mode = "country";
    $("country").value = S.regionsById[id].country;
    setArea({ region: id }, () => regionName(id), { hash: `#r=${encodeURIComponent(id)}` });
    fillRegionSelect();
    if (fit && regionsLayer) regionsLayer.eachLayer((l) => { if (l.feature.properties.id === id) fitPadded(l.getBounds()); });
  }
  function clearSelects() { $("country").value = ""; fillRegionSelect(); }

  let drawer = null, drawEndedAt = 0;
  // true while a draw tool or point mode owns map clicks (and just after a shape is finished)
  const mapPicking = () => !!drawer || S.mode === "point" || Date.now() - drawEndedAt < 500;
  function startDraw(kind) {
    stopDraw(); setPointMode(false);
    const shapeOptions = { color: C.accent, weight: 2, fillOpacity: 0.08 };
    drawer = kind === "rect" ? new L.Draw.Rectangle(map, { shapeOptions, showArea: false, metric: true })
      : new L.Draw.Polygon(map, { shapeOptions, allowIntersection: false, showArea: false, showLength: false });
    drawer.enable();
    $(kind === "rect" ? "draw-rect" : "draw-poly").classList.add("active");
  }
  function stopDraw() {
    if (drawer) { drawer.disable(); drawer = null; }
    $("draw-rect").classList.remove("active"); $("draw-poly").classList.remove("active");
  }
  map.on(L.Draw.Event.CREATED, (e) => {
    drawEndedAt = Date.now();
    stopDraw(); S.mode = "draw"; clearSelects();
    setArea({ geometry: e.layer.toGeoJSON().geometry }, () => t("custom_area"), { layer: e.layer, hash: "" });
  });
  function setPointMode(on) {
    if (on) S.mode = "point"; else if (S.mode === "point") S.mode = "country";
    $("point-pane").hidden = !on;
    $("point-btn").classList.toggle("active", on);
    map.getContainer().classList.toggle("point-mode", on);
  }
  function setPoint(lat, lon, place) {
    S.point = { lat: +lat.toFixed(3), lon: +lon.toFixed(3) }; S.place = place;
    $("lat-in").value = S.point.lat; $("lon-in").value = S.point.lon; $("coord-msg").hidden = true;
    document.querySelectorAll("#places .chip").forEach((c) => c.classList.toggle("active", !!place && c.dataset.i === String(PLACES.indexOf(place))));
    clearSelects();
    applyPoint();
  }
  function applyPoint() {
    if (!S.point) return;
    const { lat, lon } = S.point, r = S.radius;
    const grp = L.featureGroup([L.circle([lat, lon], { radius: r * 1000, color: C.accent, weight: 2, fillOpacity: 0.07 }),
      L.circleMarker([lat, lon], { radius: 3, color: C.accent, fillColor: C.accent, fillOpacity: 1 })]);
    setArea({ lat, lon, radius_km: r }, () => t("point_label", { r, name: S.place ? S.place[S.lang] : t("point_coords", { lat: fmt(lat, 2), lon: fmt(lon, 2) }) }),
      { layer: grp, hash: `#p=${lat},${lon},${r}` });
  }
  map.on("click", (e) => { if (S.mode === "point") setPoint(e.latlng.lat, e.latlng.lng, null); });
  /* typed coordinates (decimal degrees; "43,24" with a comma also works) */
  $("coords").onsubmit = (e) => {
    e.preventDefault();
    const num = (id) => parseFloat(String($(id).value).replace(",", "."));
    const lat = num("lat-in"), lon = num("lon-in"), b = S.meta && S.meta.bounds;
    const inside = b && lat >= b.south - 1 && lat <= b.north + 1 && lon >= b.west - 1 && lon <= b.east + 1;
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || !inside) {
      $("coord-msg").hidden = false;
      $("coord-msg").textContent = t("coord_bad", b ? { s: fmt(b.south, 1), n: fmt(b.north, 1), w: fmt(b.west, 1), e: fmt(b.east, 1) } : {});
      return;
    }
    setPoint(lat, lon, null);
    fitPadded(L.latLng(lat, lon).toBounds(S.radius * 2400));
  };

  /* ---------------- data ---------------- */
  async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
  async function api(path, body, as = "json") {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { const j = await r.json(); if (typeof j.detail === "string") msg = j.detail; } catch (e) { /* not json */ }
      const err = new Error(msg); err.status = r.status; throw err;
    }
    return as === "blob" ? r.blob() : r.json();
  }
  const periodBody = () => ({ ...S.area, start: `${S.from}-01-01`, end: `${S.to}-12-31` });

  async function load() {
    const seq = ++S.seq;
    status("loading");
    try {
      const key = JSON.stringify([S.area, S.from, S.to]);
      if (S.view !== "analyst" && S.dataKey !== key) {
        const [cal, an] = await Promise.all([api("/api/calendar", S.area), api("/api/analytics", periodBody())]);
        if (seq !== S.seq) return;
        S.cal = cal; S.an = an; S.dataKey = key;
      }
      if (S.view === "emergency") {
        S.activeSince = new Date(new Date(`${S.meta.date_range[1]}T00:00:00Z`).getTime() - 6 * 864e5).toISOString().slice(0, 10);
        S.active = await api("/api/events", { ...S.area, since: S.activeSince, limit: 300, sort: "area_km2" });
        const ck = cellsKey();
        if (!ck) S.cells = null;
        else if (!S.cells || S.cells.key !== ck) {
          const q = S.area.region ? `region=${encodeURIComponent(S.area.region)}` : `country=${encodeURIComponent(S.area.country)}`;
          S.cells = await getJSON(`/api/risk/cells?${q}`).then((r) => ({ ...r, key: ck })).catch(() => null);
        }
      }
      if (S.view === "analyst") {
        const q = { ...S.area, start: `${S.from}-01-01`, end: `${S.to}-12-31`, types: [...S.evTypes], min_area_km2: S.evMin };
        const [top, sample] = await Promise.all([api("/api/events", { ...q, sort: S.evSort, desc: true, limit: 500 }),
          api("/api/events", { ...q, sort: "random", limit: 500 })]);
        S.events = top;
        const seen = new Set(top.events.map((e) => e.id));
        S.evScatter = top.events.concat(sample.events.filter((e) => !seen.has(e.id)));
      }
      if (seq !== S.seq) return;
      status(null);
      render();
      $("sheet-scroll").scrollTop = 0;
    } catch (e) {
      if (seq === S.seq) status("error", e.message);
    }
  }
  function status(kind, msg) {
    clearTimeout(S.slowTimer);
    $("panels").classList.toggle("is-loading", kind === "loading");
    const el = $("status");
    if (!kind) { el.hidden = true; return; }
    el.hidden = false;
    el.classList.toggle("error", kind === "error");
    $("retry").hidden = kind !== "error";
    $("status-text").textContent = kind === "loading" ? t("loading") : `${t("error")} ${msg || ""}`;
    if (kind === "loading") S.slowTimer = setTimeout(() => { $("status-text").textContent = t("loading_slow"); }, 2500);
  }

  /* ---------------- tabs ---------------- */
  function renderTabs() {
    $("view-hint").textContent = t(`hint_${S.view}`);
    $("tabs").innerHTML = TABS[S.view].map((k) => `<button type="button" role="tab" data-tab="${k}" aria-selected="${S.tab[S.view] === k}">${esc(t(`tab_${k}`))}</button>`).join("");
    applyPanels();
  }
  function applyPanels() {
    const key = `${S.view}:${S.tab[S.view]}`;
    document.body.dataset.tab = S.tab[S.view];
    document.querySelectorAll("#panels [data-panel]").forEach((el) => { el.hidden = !el.dataset.panel.split(" ").includes(key); });
    setTimeout(resizeCharts, 0);
    if (S.tab[S.view] === "data") renderRatio();
  }
  $("tabs").onclick = (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    S.tab[S.view] = b.dataset.tab;
    document.querySelectorAll("#tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    applyPanels();
    $("sheet-scroll").scrollTop = 0;
  };

  /* ---------------- charts ---------------- */
  const CH = {};
  ["ytd-chart", "trend-chart", "cal-chart", "ev-year-chart", "ev-scatter"].forEach((id) => { CH[id] = echarts.init($(id)); });
  function resizeCharts() { Object.values(CH).forEach((c) => c && c.resize()); if (ratioChart) ratioChart.resize(); }
  /* PNG export: every chart gets a small button; the image carries the block title, the area and the source */
  function chartPNG(id) {
    const ch = id === "ratio-chart" ? ratioChart : CH[id];
    if (!ch) return;
    const block = $(id).closest(".block, .method-body > div");
    const h = block && block.querySelector("h2, h3");
    const title = h ? h.textContent.trim() : "";
    const sub = `${S.labelFn ? S.labelFn() : t("all_ca")} · FireCalendar · NASA FIRMS MODIS + VIIRS`;
    const img = new Image();
    img.onload = () => {
      const pr = 2, pad = 16 * pr, head = 54 * pr, cv = document.createElement("canvas");
      cv.width = img.width + pad * 2; cv.height = img.height + head + pad;
      const g = cv.getContext("2d");
      g.fillStyle = C.panel; g.fillRect(0, 0, cv.width, cv.height);
      g.fillStyle = C.text; g.font = `600 ${15 * pr}px ${C.body}`; g.fillText(title, pad, pad + 14 * pr);
      g.fillStyle = C.muted; g.font = `${11 * pr}px ${C.mono}`; g.fillText(sub, pad, pad + 32 * pr);
      g.drawImage(img, pad, head);
      cv.toBlob((bl) => save(bl, `${id.replace("-chart", "")}_${slug()}.png`), "image/png");
    };
    img.src = ch.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: C.panel });
  }
  /* explanations of charts are folded under "How to read this": newcomers see answers first, details on demand */
  document.querySelectorAll("#panels p.desc").forEach((p) => {
    if (["risk-desc", "active-desc"].includes(p.id) || p.closest(".alerts-block")) return;
    const d = document.createElement("details"), s = document.createElement("summary");
    d.className = "howto"; s.dataset.i18n = "how_read";
    p.replaceWith(d); d.append(s, p);
  });
  /* every report block with a title can be folded to its header; the site opens with all of them folded
     (only the short answer is shown), the reader opens what interests them */
  document.querySelectorAll("#panels > section.block").forEach((sec) => {
    const h = sec.querySelector(":scope > h2, :scope > .block-head > h2");
    if (!h) return;
    let head = h.parentElement.classList.contains("block-head") ? h.parentElement : null;
    if (!head) { head = document.createElement("div"); head.className = "block-head"; h.replaceWith(head); head.append(h); }
    head.classList.add("fold-head");
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "fold-btn";
    btn.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;
    head.append(btn);
    const set = (folded) => {
      sec.classList.toggle("folded", folded);
      btn.setAttribute("aria-expanded", String(!folded));
      btn.dataset.i18nTitle = folded ? "fold_show" : "fold_hide";
      btn.title = t(btn.dataset.i18nTitle); btn.setAttribute("aria-label", btn.title);
      if (!folded) setTimeout(resizeCharts, 0);
    };
    btn.onclick = () => set(!sec.classList.contains("folded"));
    h.onclick = () => set(!sec.classList.contains("folded"));
    set(true);
  });
  ["ytd-chart", "trend-chart", "cal-chart", "ev-year-chart", "ev-scatter", "ratio-chart"].forEach((id) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "png-btn"; b.dataset.chart = id; b.dataset.i18n = "dl_png";
    b.onclick = () => chartPNG(id);
    $(id).insertAdjacentElement("afterend", b);
  });
  window.addEventListener("resize", resizeCharts);
  const axis = () => ({
    axisLine: { lineStyle: { color: C.line2 } }, axisTick: { show: false },
    axisLabel: { color: C.muted, fontSize: 10.5, fontFamily: C.mono }, splitLine: { lineStyle: { color: C.line, type: [2, 3] } },
  });
  const tooltip = () => ({ backgroundColor: C.panel, borderColor: C.line2, borderRadius: 0, padding: [7, 10], confine: true,
    textStyle: { color: C.text, fontSize: 12, fontFamily: C.body }, extraCssText: "box-shadow:none;" });
  const legend = () => ({ top: 0, left: 0, icon: "rect", itemWidth: 10, itemHeight: 10, itemGap: 12, textStyle: { color: C.text2, fontSize: 11, fontFamily: C.mono } });
  const kfmt = (v) => (Math.abs(v) >= 1000 ? `${fmt(v / 1000, 1)}k` : fmt(v));
  const base = () => ({ animation: false, textStyle: { fontFamily: C.mono } });

  /* ================= render ================= */
  function pickerSummary() { $("picker-sum").textContent = `${t(`view_${S.view}`)} · ${S.labelFn ? S.labelFn() : t("all_ca")}`; }
  function foldPicker(folded) {
    document.body.classList.toggle("picker-folded", folded);
    $("picker-toggle").setAttribute("aria-expanded", String(!folded));
    pickerSummary();
    if (isPhone()) setTimeout(refit, 60);
  }
  $("picker-toggle").onclick = () => foldPicker(!document.body.classList.contains("picker-folded"));
  function render() {
    $("area-name").textContent = S.labelFn ? S.labelFn() : t("all_ca");
    pickerSummary();
    renderLegend();
    restyleMap();
    if (S.view !== "analyst") {
      const empty = S.cal && S.cal.stats.points === 0;
      $("empty").hidden = !empty;
      if (empty) return;
      renderAnswer();
    } else $("empty").hidden = true;
    if (S.view === "overview") { renderYtd(); renderSeason(); renderTrend(); renderCalendar(); eventsLayer.clearLayers(); }
    if (S.view === "emergency") renderEmergency();
    if (S.view === "analyst") renderAnalyst();
    $("dl-events").hidden = S.view !== "analyst";
    setTimeout(resizeCharts, 0);
    document.dispatchEvent(new CustomEvent("fc:render"));
  }

  /* ---------- answer (overview + emergency) ---------- */
  /* the answer follows the time picked on the map (year from the selector / play, month from the calendar) */
  const pctl = (a, q) => { const x = [...a].sort((m, n) => m - n); if (!x.length) return null; const i = (x.length - 1) * q, lo = Math.floor(i); return x[lo] + (x[Math.ceil(i)] - x[lo]) * (i - lo); };
  function pickedPeriod() {
    const c = S.cal;
    if (S.mapMonth) {
      const yi = c.years.indexOf(S.mapMonth.y), m = S.mapMonth.m - 1, v = yi >= 0 ? c.harmonized[yi][m] : null;
      if (v == null) return null;
      return { when: t("month_full", { m: MF(m), y: S.mapMonth.y }), v, med: c.monthly_median[m], lo: c.monthly_p10[m], hi: c.monthly_p90[m],
        anom: c.anomaly_pct[yi][m], ctx: t("for_month") };
    }
    if (S.mapYear) {
      const y = +S.mapYear, yi = c.years.indexOf(y);
      if (yi < 0) return null;
      const partial = y === S.meta.last_year && S.meta.last_full_year < y;
      if (partial) {
        const a = S.an.ytd;
        if (!a || a.year !== y) return null;
        const k = a.through_doy - 1, v = a.current[a.current.length - 1], med = a.p50[k];
        return { when: t("year_partial", { y, d: dateFmt(S.meta.date_range[1]) }), v, med, lo: a.p10[k], hi: a.p90[k],
          anom: med > 0 ? ((v - med) / med) * 100 : null, ctx: t("for_dates") };
      }
      const full = S.meta.years_full.filter((yy) => yy <= S.meta.last_full_year).map((yy) => c.annual[c.years.indexOf(yy)]).filter((x) => x != null);
      const v = c.annual[yi], med = pctl(full, 0.5);
      return { when: t("year_label", { y }), v, med, lo: pctl(full, 0.1), hi: pctl(full, 0.9), anom: med > 0 ? ((v - med) / med) * 100 : null, ctx: t("for_year") };
    }
    return null;
  }
  function renderAnswer() {
    const st = S.cal.stats, mtd = st.month_to_date, last = st.latest, pk = pickedPeriod();
    let when, v, lo = null, hi = null, anom, ctx, med;
    $("ans-eyebrow").textContent = pk ? t("picked_period") : t("now");
    $("back-now").hidden = !pk; $("back-now").textContent = t("back_now");
    if (pk) ({ when, v, lo, hi, anom, ctx, med } = pk);
    else if (mtd) { when = t("dates_partial", { m: MG(mtd.month - 1), d: mtd.day, y: mtd.year }); v = mtd.value; lo = mtd.p10; hi = mtd.p90; anom = mtd.anomaly_pct; ctx = t("for_dates"); med = mtd.median; }
    else { when = t("month_full", { m: MF(last.month - 1), y: last.year }); v = last.value; anom = last.anomaly_pct; ctx = t("for_month"); med = last.median; }
    const cmp = compare(anom, v, med, lo, hi);
    $("ans-main").innerHTML = `${esc(when)}: <b>${esc(hs(v))}</b> — <span class="${cmp.cls}">${esc(cmp.text)}</span> ${esc(ctx)}.`;
    $("band").hidden = lo == null;
    if (lo != null) {
      const max = Math.max(hi, v, 1) * 1.15;
      $("band-normal").style.left = `${(lo / max) * 100}%`;
      $("band-normal").style.width = `${Math.max(((hi - lo) / max) * 100, 0.8)}%`;
      $("band-mark").style.left = `${Math.min(v / max, 1) * 100}%`;
      $("band-lo").textContent = "0";
      $("band-hi").textContent = fmt(max);
      $("band-caption").textContent = `${t("normal_range")}: ${fmt(lo)}–${fmt(hi)}`;
    }
    const y = S.an.ytd;
    $("f-ytd").innerHTML = y && y.year === S.meta.last_year ? `${vsNormal(y.current_vs_median_pct)}<small>${esc(hs(y.current[y.current.length - 1]))}</small>` : "–";
    $("f-last-l").textContent = t("month_full", { m: MI(last.month - 1), y: last.year });
    $("f-last").innerHTML = `${vsNormal(last.anomaly_pct, last.median)}<small>${esc(hs(last.value))}</small>`;
    const fs = S.an.fire_season;
    $("f-season").innerHTML = fs ? `${esc(`${M(fs.start_month - 1)}–${M(fs.end_month - 1)}`)}<small>${fmt(fs.share * 100)}% · ${esc(MI(fs.peak_month - 1))}</small>` : "–";
  }

  /* ---------- overview charts ---------- */
  function renderYtd() {
    const y = S.an.ytd, ch = CH["ytd-chart"];
    if (!y) { ch.clear(); $("ytd-ref").textContent = ""; return; }
    const n = y.p50.length, day = (i) => new Date(Date.UTC(y.year, 0, 1 + i));
    const ax = axis();
    ch.setOption({
      ...base(), grid: { left: 42, right: 8, top: 30, bottom: 22 },
      legend: { ...legend(), data: [String(y.year), t("ytd_med"), t("ytd_band")] },
      tooltip: { ...tooltip(), trigger: "axis", axisPointer: { type: "line", lineStyle: { color: C.line2 } },
        formatter: (ps) => { const i = ps[0].dataIndex, d = day(i), cur = y.current[i];
          return `<b>${t("date_fmt", { d: d.getUTCDate(), m: MG(d.getUTCMonth()), y: y.year })}</b><br>` + (cur != null ? `${y.year}: <b>${fmt(cur)}</b><br>` : "")
            + `${t("ytd_med")}: ${fmt(y.p50[i])}<br>${t("ytd_band")}: ${fmt(y.p10[i])}–${fmt(y.p90[i])}`; } },
      xAxis: { type: "category", data: Array.from({ length: n }, (_, i) => i), boundaryGap: false, ...ax, splitLine: { show: false },
        axisLabel: { ...ax.axisLabel, interval: (i) => day(i).getUTCDate() === 1 && day(i).getUTCFullYear() === y.year && day(i).getUTCMonth() % 2 === 0, formatter: (i) => M(day(+i).getUTCMonth()) } },
      yAxis: { type: "value", ...ax, axisLabel: { ...ax.axisLabel, formatter: kfmt } },
      series: [
        { name: "p10", type: "line", data: y.p10, stack: "b", symbol: "none", lineStyle: { opacity: 0 }, silent: true },
        { name: t("ytd_band"), type: "line", data: y.p90.map((v, i) => v - y.p10[i]), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: C.band, opacity: 0.45 }, itemStyle: { color: C.band }, silent: true },
        { name: t("ytd_med"), type: "line", data: y.p50, symbol: "none", lineStyle: { color: C.text2, type: [4, 3], width: 1.2 }, itemStyle: { color: C.text2 } },
        { name: String(y.year), type: "line", data: y.current, symbol: "none", lineStyle: { color: C.accent, width: 2.4 }, itemStyle: { color: C.accent },
          markPoint: { symbol: "rect", symbolSize: 7, itemStyle: { color: C.accent }, label: { show: false }, data: [{ coord: [y.current.length - 1, y.current[y.current.length - 1]] }] } },
      ],
    }, true);
    const ref = y.reference_years;
    $("ytd-ref").textContent = ref.length ? t("ytd_ref", { n: ref.length, a: ref[0], b: ref[ref.length - 1] }) : "";
  }
  function renderSeason() {
    const fs = S.an.fire_season, el = $("season");
    if (!fs) { el.innerHTML = ""; $("season-text").textContent = "–"; return; }
    $("season-text").textContent = t("season_text", { label: `${MF(fs.start_month - 1)}–${MI(fs.end_month - 1)}`, share: fmt(fs.share * 100), peak: MI(fs.peak_month - 1) });
    const inS = new Set(Array.from({ length: fs.length_months }, (_, k) => (fs.start_month - 1 + k) % 12));
    const max = Math.max(...fs.monthly_share, 1e-9);
    el.innerHTML = fs.monthly_share.map((s, i) => `<div class="season-m${inS.has(i) ? " in-season" : ""}" role="listitem" title="${esc(`${MF(i)}: ${fmt(s * 100, 1)}% · ${t(`risk_${fs.risk[i]}`)}`)}">
      <span class="season-pct">${fmt(s * 100)}</span><div class="season-bar ${fs.risk[i]}" style="height:${Math.max((s / max) * 66, 2)}px"></div><span class="season-lbl">${esc(M(i))}</span></div>`).join("");
  }
  function annualHalfWidth(year) {
    const c = S.cal, yi = c.years.indexOf(year);
    if (yi < 0) return 0;
    let s = 0;
    for (let m = 0; m < 12; m++) { const h = c.harmonized[yi][m], hi = c.harmonized_hi[yi][m]; if (h != null && hi != null) s += (hi - h) ** 2; }
    return Math.sqrt(s);
  }
  function rawAnnual(key, year) { const row = S.cal[key][S.cal.years.indexOf(year)]; return row && row.every((v) => v != null) ? row.reduce((a, b) => a + b, 0) : null; }
  function renderTrend() {
    const an = S.an, tr = an.trend, tv = an.trend_viirs_only, ch = CH["trend-chart"];
    const yrs = an.annual.map((a) => a.year), vals = an.annual.map((a) => a.hotspots), hw = yrs.map(annualHalfWidth);
    if (tr) {
      const word = tr.direction === "decreasing" ? t("trend_down") : tr.direction === "increasing" ? t("trend_up") : t("trend_flat");
      const cls = tr.significant_95 ? (tr.direction === "decreasing" ? "down" : "up") : "";
      $("trend-text").innerHTML = `<span class="${cls}">${esc(word)}</span> · ${esc(t(tr.significant_95 ? "trend_sig" : "trend_nonsig", { p: signed(tr.pct_per_decade ?? 0) }))} <span class="note">p ${esc(pfmt(tr.p_value))}</span>`;
    } else $("trend-text").textContent = t("trend_none");
    const showV = tr && tv && yrs.length && yrs[0] < 2012;
    $("trend-viirs").hidden = !showV;
    if (showV) $("trend-viirs").textContent = t("trend_viirs", { p: pfmt(tv.p_value), v: t(tv.significant_95 ? "trend_viirs_ok" : "trend_viirs_weak") });
    let line = null;
    if (tr && yrs.length) { const sl = tr.sen_slope_per_year, ic = median(vals.map((v, i) => v - sl * yrs[i])); line = yrs.map((y) => Math.max(ic + sl * y, 0)); }
    const ax = axis();
    const series = [
      { name: t("s_harm"), type: "bar", data: vals, barMaxWidth: 14, color: C.accent, itemStyle: { color: (p) => (yrs[p.dataIndex] < 2012 ? `${C.accent}99` : C.accent) } },
      { name: t("s_range"), type: "custom", z: 3, color: C.text2, tooltip: { show: false },
        data: yrs.map((y, i) => [i, Math.max(vals[i] - hw[i], 0), vals[i] + hw[i], hw[i]]).filter((d) => d[3] > 0),
        renderItem: (params, a2) => { const x = a2.value(0), lo = a2.coord([x, a2.value(1)]), hi = a2.coord([x, a2.value(2)]); const w = a2.size([1, 0])[0] * 0.2, st = { stroke: C.text2, lineWidth: 1 };
          return { type: "group", children: [{ type: "line", shape: { x1: lo[0], y1: lo[1], x2: hi[0], y2: hi[1] }, style: st },
            { type: "line", shape: { x1: lo[0] - w, y1: lo[1], x2: lo[0] + w, y2: lo[1] }, style: st }, { type: "line", shape: { x1: hi[0] - w, y1: hi[1], x2: hi[0] + w, y2: hi[1] }, style: st }] }; } },
    ];
    if (line) series.push({ name: t("s_trend"), type: "line", data: line, symbol: "none", lineStyle: { color: C.text, width: 1.5, type: [5, 4] }, itemStyle: { color: C.text } });
    if (S.showRaw) {
      series.push({ name: t("s_modis"), type: "line", data: yrs.map((y) => rawAnnual("raw_modis", y)), symbol: "none", lineStyle: { color: C.modis, width: 1.8 }, itemStyle: { color: C.modis } });
      series.push({ name: t("s_viirs"), type: "line", data: yrs.map((y) => (y >= 2012 ? rawAnnual("raw_viirs", y) : null)), symbol: "none", lineStyle: { color: C.viirs, width: 1.8 }, itemStyle: { color: C.viirs } });
    }
    ch.setOption({
      ...base(), grid: { left: 42, right: 8, top: S.showRaw ? 52 : 30, bottom: 22 }, legend: legend(),
      tooltip: { ...tooltip(), trigger: "axis", axisPointer: { type: "line", lineStyle: { color: C.line2 } },
        formatter: (ps) => { const i = ps[0].dataIndex; let s = `<b>${yrs[i]}</b><br>${t("s_harm")}: <b>${fmt(vals[i])}</b>`;
          if (hw[i] > 0) s += `<br>${t("s_range")}: ${fmt(Math.max(vals[i] - hw[i], 0))}–${fmt(vals[i] + hw[i])}`;
          ps.filter((p) => p.seriesType === "line" && p.value != null).forEach((p) => { s += `<br>${p.seriesName}: ${fmt(p.value)}`; }); return s; } },
      xAxis: { type: "category", data: yrs, ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, interval: (i) => i % 4 === 0 } },
      yAxis: { type: "value", ...ax, axisLabel: { ...ax.axisLabel, formatter: kfmt } },
      series,
    }, true);
  }
  const SRC = { viirs: "src_viirs", modis: "src_modis", modis_terra: "src_modis_terra", mixed: "src_mixed", recent: "src_recent" };
  function renderCalendar() {
    const c = S.cal, mode = S.calMode, ch = CH["cal-chart"];
    $("cal-desc").textContent = mode === "anom" ? t("cal_desc_anom") : t("cal_desc_count");
    document.querySelectorAll("#cal-modes button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.cal === mode)));
    const cells = [];
    c.years.forEach((y, yi) => { for (let mi = 0; mi < 12; mi++) { const v = c.harmonized[yi][mi]; if (v == null) continue;
      cells.push([mi, yi, mode === "anom" ? Math.log2((v + 1) / ((c.monthly_median[mi] ?? 0) + 1)) : Math.log10(v + 1), v]); } });
    const logs = cells.map((x) => x[2]).sort((a, b) => a - b), q = (p) => logs[Math.min(logs.length - 1, Math.floor(p * logs.length))] ?? 0;
    const lo = q(0.03), hi = Math.max(q(0.99), lo + 0.5);
    const visual = mode === "anom" ? { min: -3, max: 3, inRange: { color: DIVERGE }, text: [t("scale_more"), t("scale_less")] }
      : { min: lo, max: hi, inRange: { color: C.ramp }, text: [`${fmt(10 ** hi - 1)}+`, fmt(10 ** lo - 1)] };
    const vr = c.years.indexOf(2012), ax = axis();
    ch.setOption({
      ...base(), grid: { left: 38, right: 4, top: 22, bottom: 44 },
      tooltip: { ...tooltip(), formatter: (p) => { if (p.seriesType !== "heatmap") return ""; const [mi, yi, , v] = p.data;
        const lo90 = c.harmonized_lo[yi][mi], hi90 = c.harmonized_hi[yi][mi], an = c.anomaly_pct[yi][mi], src = c.source[yi][mi];
        let s = `<b>${t("month_full", { m: MF(mi), y: c.years[yi] })}</b><br>${t("tt_harm")}: <b>${fmt(v)}</b>`;
        if (hi90 > v) s += `<br>${t("tt_range")}: ${fmt(lo90)}–${fmt(hi90)}`;
        s += `<br>${t("tt_typical", { m: M(mi) })}: ${fmt(c.monthly_median[mi])}`;
        if (an != null) s += `<br>${t("tt_vs")}: <b>${signed(an)}%</b>`;
        if (src) s += `<br><span style="color:${C.muted}">${t(SRC[src])}</span>`;
        return `${s}<br><span style="color:${C.muted}">${t("tt_click")}</span>`; } },
      xAxis: { type: "category", data: D().months, position: "top", ...ax, axisLine: { show: false }, splitLine: { show: false } },
      yAxis: { type: "category", data: c.years, inverse: true, ...ax, axisLine: { show: false }, splitLine: { show: false } },
      visualMap: { ...visual, calculable: false, orient: "horizontal", left: 0, bottom: 0, itemHeight: 180, itemWidth: 8, dimension: 2,
        textStyle: { color: C.muted, fontSize: 10.5, fontFamily: C.mono } },
      series: [{ type: "heatmap", data: cells, cursor: "pointer", itemStyle: { borderColor: C.panel, borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: C.text, borderWidth: 1 } },
        markLine: vr > 0 ? { silent: true, symbol: "none", lineStyle: { color: C.text2, type: [3, 3], width: 1 },
          label: { color: C.text2, fontSize: 10, fontFamily: C.mono, formatter: t("viirs_era"), position: "insideEndTop" }, data: [{ yAxis: vr - 0.5 }] } : undefined }],
    }, true);
  }
  CH["cal-chart"].on("click", (p) => {
    if (p.seriesType !== "heatmap") return;
    stopPlay(); S.mapMonth = { y: S.cal.years[p.data[1]], m: p.data[0] + 1 };
    $("clear-month").hidden = false; showGrid();
    if (isPhone()) $("sheet").classList.remove("expanded");
  });

  /* ---------- compact lists ---------- */
  function moreButton(holder, total, shown, key, step) {
    if (!holder) return;
    let label = "";
    if (step != null) label = total > shown ? t("show_more", { n: fmt(Math.min(step, total - shown)) }) : "";
    else if (S.open[key]) label = total > 5 ? t("show_less") : "";
    else if (total > shown) label = t("show_all", { n: fmt(total) });
    holder.innerHTML = label ? `<button type="button" class="more-btn" data-k="${key}">${esc(label)}</button>` : "";
  }
  function eventRow(e, i) {
    return `<button type="button" class="row" data-i="${i}"><span class="r-main"><i class="t-${e.type}"></i><span class="r-txt">
      <b>${esc(regionName(e.region))}</b><small>${esc(dRange(e))} · ${esc(t(`type_${e.type}`))}</small></span></span>
      <span class="r-val">${fmt(e.area_km2, 1)} km²</span></button>`;
  }

  /* ---------- emergency ---------- */
  const inArea = (r) => { const c = selCountry(); return c ? r.country === c : true; };
  function focusRegionId() {
    if (S.area.region) return S.area.region;
    const list = S.regions.filter(inArea).filter((r) => S.risk.regions[r.id]).sort((a, b) => S.risk.regions[b.id].prob - S.risk.regions[a.id].prob);
    return list[0] ? list[0].id : null;
  }
  function renderEmergency() {
    const rk = S.risk, v = rk.validation, fid = focusRegionId();
    $("risk-desc").textContent = t("risk_desc", { d: dateFmt(rk.issue_date) });
    let head = "";
    if (!S.area.region) {
      const inA = S.regions.filter(inArea).filter((r) => rk.regions[r.id]);
      head += `<p class="desc">${esc(t("country_risk", { n: inA.filter((r) => rk.regions[r.id].prob >= 0.2).length, m: inA.length }))}</p>`;
    }
    if (fid) {
      const k = rk.regions[fid];
      const drv = (k.drivers.length ? k.drivers.map((d) => `<span>${esc(t(`drv_${d.key}`))}</span>`) : [`<span>${esc(t("drv_none"))}</span>`]).join("");
      head += `<div class="risk-card${S.area.region ? "" : " pick"}" data-id="${esc(fid)}">
        <div class="rc-top"><span class="rc-name">${esc(regionName(fid))}</span><span class="lvl lvl-${k.level}">${esc(t(`lvl_${k.level}`))}</span></div>
        <div class="rc-top"><span class="rc-prob">${pct(k.prob)}</span>${k.times_usual ? `<span class="rc-sub">${esc(t("times_usual", { x: fmt(k.times_usual, 1) }))}</span>` : ""}</div>
        <div class="drivers">${drv}</div></div>`;
      if (!S.area.region) head += `<p class="note">${esc(t("focus_hint"))}</p>`;
    }
    if (cellsKey() && S.cells && S.cells.key === cellsKey() && S.cells.cells.length) {
      const top = S.cells.cells.slice(0, 5);
      head += `<h3 class="places-h">${esc(t("top_places"))}</h3><ol class="rows rank places">${top.map((c, i) => `<li class="row" data-cell="${i}">
        <span class="r-txt"><b>${esc(S.area.region ? partOf(c.lat + S.cells.cell_deg / 2, c.lon + S.cells.cell_deg / 2) : regionName(c.region))}</b><small>${fmt(c.lat + 0.125, 2)}°, ${fmt(c.lon + 0.125, 2)}° · ${esc(c.drivers.map((d) => t(`cdrv_${d}`)).join(", ") || t("drv_none"))}</small></span>
        <span class="lvl lvl-${cellLevel(c.prob)}">${esc(t(`lvl_${cellLevel(c.prob)}`))}</span><span class="r-val">${pct(c.prob)}</span></li>`).join("")}</ol>
        <p class="note">${esc(t("cells_note", { a: fmt(S.cells.validation.auc, 2), c: fmt(S.cells.validation.auc_place_climatology, 2) }))}</p>`;
    }
    $("risk-head").innerHTML = head;
    const a = S.active, nA = S.open.active ? a.events.length : Math.min(5, a.events.length);
    $("active-desc").textContent = t("active_desc", { d: dateFmt(S.activeSince) });
    $("active-list").innerHTML = a.events.length ? a.events.slice(0, nA).map(eventRow).join("") + `<div id="active-more"></div>` : `<p class="desc">${esc(t("active_none"))}</p>`;
    if (a.events.length) moreButton($("active-more"), a.events.length, nA, "active");
    drawEvents(S.emMode === "fires" ? a.events.slice(0, 300) : []);
    restyleMap(); drawCells();
    $("risk-why").innerHTML = fid ? whyHTML(fid) : `<p class="desc">${esc(t("focus_pick"))}</p>`;
    $("risk-act").innerHTML = fid ? actHTML(fid) : `<p class="desc">${esc(t("focus_pick"))}</p>`;
    if (fid) loadBriefing(fid);
    const list = S.regions.filter(inArea).filter((r) => rk.regions[r.id]).sort((x, y) => rk.regions[y.id].prob - rk.regions[x.id].prob);
    const nR = S.open.risk ? list.length : Math.min(8, list.length);
    $("risk-list").innerHTML = list.slice(0, nR).map((r) => { const k = rk.regions[r.id];
      return `<li class="row${r.id === fid ? " sel" : ""}" data-id="${esc(r.id)}"><span class="r-txt"><b>${esc(regionName(r.id))}</b><small>${esc(countryName(r.country))}</small></span>
        <span class="lvl lvl-${k.level}">${esc(t(`lvl_${k.level}`))}</span><span class="r-val">${pct(k.prob)}</span></li>`; }).join("");
    $("risk-list").insertAdjacentHTML("afterend", "");
    let holder = $("risk-more");
    if (!holder) { holder = document.createElement("div"); holder.id = "risk-more"; $("risk-list").after(holder); }
    moreButton(holder, list.length, nR, "risk");
    $("risk-reliability").textContent = t("reliability", { h: fmt((v.hit_rate_when_high || 0) * 100), b: fmt(v.event_rate_test * 100), a: fmt(v.auc, 2), s: fmt(v.ablation.season_only.auc, 2) });
  }
  function whyHTML(id) {
    const k = S.risk.regions[id], n = k.now;
    const sg = (x, d = 1) => `${x > 0 ? "+" : ""}${fmt(x, d)}`;
    const w = [[t("w_temp"), `${sg(n.tmax30_anom_c)} °C`], [t("w_rain"), t("pct_of_normal", { v: fmt(n.rain30_pct_of_normal) })],
      [t("w_rh"), `${sg(n.rh30_anom)} ${t("unit_pp")}`], [t("w_soil"), `${sg(n.soil_wetness_anom * 100)} ${t("unit_pp")}`],
      ...(n.rain180_pct_of_normal != null ? [[t("w_rain180"), t("pct_of_normal", { v: fmt(n.rain180_pct_of_normal) })], [t("w_vpd"), `${sg(n.vpd30_anom, 2)} kPa`],
        [t("w_root"), `${sg(n.rootzone_wetness_anom * 100)} ${t("unit_pp")}`], [t("w_nbr"), fmt(n.neighbors_fires14)]] : []),
      [t("w_wind"), `${sg(n.wind14_anom)} m/s`], [t("w_fires"), fmt(n.fires_last14), n.typical_next14 != null ? t("typical_2w", { v: fmt(n.typical_next14) }) : ""]]
      .map(([a, b, c]) => `<div><dt>${esc(a)}</dt><dd>${esc(b)}${c ? `<small>${esc(c)}</small>` : ""}</dd></div>`).join("");
    const drivers = k.drivers.length ? k.drivers.map((d) => `<span>${esc(t(`drv_${d.key}`))}</span>`).join("") : `<span>${esc(t("drv_none"))}</span>`;
    const analogs = k.analogs.map((a) => `<li>${esc(t("analog_row", { d: dateFmt(a.date), n: hs(a.fires_next14), t: a.typical == null ? "–" : fmt(a.typical) }))}</li>`).join("");
    return `<div class="ai-brief" data-brief="why"></div><div class="block"><h2>${esc(regionName(id))}</h2><h3>${esc(t("why"))}</h3><div class="drivers">${drivers}</div>
      <h3>${esc(t("weather_now"))}</h3><dl class="weather-grid">${w}</dl>
      ${analogs ? `<h3>${esc(t("analogs_title"))}</h3><ul class="plain analogs">${analogs}</ul>` : ""}</div>`;
  }
  function actHTML(id) {
    const k = S.risk.regions[id], r = S.regionsById[id];
    const acts = [...(D()[`act_${k.level}`] || I18N.en[`act_${k.level}`])];
    if (r && r.events_recent && r.events_recent.recurrent_share >= 0.5) acts.push(t("act_recurrent"));
    if (r && r.events_recent && r.events_recent.large >= 3) acts.push(t("act_large"));
    return `<div class="ai-brief" data-brief="act"></div><div class="block"><div class="block-head"><h2>${esc(regionName(id))}</h2><span class="lvl lvl-${k.level}">${esc(t(`lvl_${k.level}`))}</span></div>
      <details class="generic-acts"><summary>${esc(t("ai_generic"))}</summary><ul class="plain">${acts.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details></div>`;
  }
  /* AI briefing (Claude, from this province's facts only): situation, places to watch, concrete actions */
  function briefHTML(kind, b) {
    const chev = `<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;
    const note = `<p class="note ai-note">${esc(t("ai_brief_note", { d: dateFmt(b.issue_date) }))}</p>`;
    const head = (title) => `<summary class="ai-head"><span class="ai-tag">${esc(t("ai_tag"))}</span><span class="ai-title">${esc(title)}</span>${chev}</summary>`;
    const sec = (title, body, extra = "") => `<details class="ai-sec" open><summary>${esc(title)}${extra}${chev}</summary><div class="ai-body">${body}</div></details>`;
    if (kind === "why") {
      const watch = b.watch.map((w, i) => `<details class="ai-item"><summary><span class="ai-num">${i + 1}</span><span class="ai-where">${esc(w.where)}</span>${chev}</summary><p>${esc(w.why)}</p></details>`).join("");
      return `<details class="ai-card" open>${head(t("ai_brief_title"))}<div class="ai-inner">
        ${sec(t("ai_situation"), `<p class="lead">${esc(b.headline)}</p><p>${esc(b.situation)}</p>`)}
        ${b.watch.length ? sec(t("ai_watch"), `<div class="ai-items">${watch}</div>`, `<span class="ai-count">${b.watch.length}</span>`) : ""}
        ${note}</div></details>`;
    }
    const acts = b.actions.map((a, i) => `<details class="ai-item" open><summary><span class="ai-num">${i + 1}</span><span class="ai-where">${esc(a.what)}</span>${chev}</summary>
      <div class="aa-meta"><span><small>${esc(t("ai_who"))}</small>${esc(a.who)}</span><span><small>${esc(t("ai_when"))}</small>${esc(a.when)}</span></div></details>`).join("");
    return `<details class="ai-card" open>${head(t("ai_actions_title"))}<div class="ai-inner">
      ${sec(t("ai_actions"), `<div class="ai-items">${acts}</div>`, `<span class="ai-count">${b.actions.length}</span>`)}
      ${sec(t("ai_public"), `<blockquote class="ai-public">${esc(b.public_message)}</blockquote><button type="button" class="btn ai-copy">${esc(t("ai_copy"))}</button>`)}
      ${note}</div></details>`;
  }
  function paintBriefing(id) {
    const b = S.brief[`${id}|${S.lang}`];
    document.querySelectorAll(".ai-brief[data-brief]").forEach((el) => {
      if (!b) return;
      if (b === "loading") el.innerHTML = `<div class="ai-card ai-loading"><div class="ai-head"><span class="ai-tag">${esc(t("ai_tag"))}</span><span class="ai-title">${esc(t("ai_brief_title"))}</span></div><p class="note"><span class="spinner"></span> ${esc(t("ai_brief_loading"))}</p></div>`;
      else if (b.error) el.innerHTML = `<p class="note">${esc(t("ai_brief_err"))}</p>`;
      else el.innerHTML = briefHTML(el.dataset.brief, b);
    });
    document.querySelectorAll(".ai-copy").forEach((btn) => {
      btn.onclick = async () => {
        const txt = btn.parentElement.querySelector(".ai-public").textContent;
        try { await navigator.clipboard.writeText(txt); btn.textContent = t("ai_copied"); } catch (e) { /* clipboard blocked */ }
      };
    });
  }
  async function loadBriefing(id) {
    const key = `${id}|${S.lang}`;
    if (!S.brief[key]) {
      S.brief[key] = "loading";
      getJSON(`/api/ai/briefing?region=${encodeURIComponent(id)}&lang=${S.lang}`)
        .then((b) => { S.brief[key] = b; }).catch(() => { S.brief[key] = { error: true }; })
        .finally(() => { if (focusRegionId() === id && S.view === "emergency") paintBriefing(id); });
    }
    paintBriefing(id);
  }
  function drawEvents(list) {
    eventsLayer.clearLayers();
    for (const e of list) {
      L.circleMarker([e.lat, e.lon], { radius: Math.max(3, Math.min(14, Math.sqrt(e.area_km2) * 1.2)), color: C.panel, weight: 1, fillColor: TYPE_COLOR[e.type], fillOpacity: 0.9 })
        .bindPopup(eventPopup(e)).addTo(eventsLayer);
    }
  }
  const eventPopup = (e) => `<b>${esc(t(`type_${e.type}`))}</b><br>${esc(regionName(e.region))}<br><span class="num">${esc(t("ev_popup", { r: dRange(e), a: fmt(e.area_km2, 1), d: e.duration_days, f: fmt(e.frp_max_mw) }))}</span>${window.FC && FC.fireLink ? FC.fireLink(e.id) : ""}`;
  function flyToEvent(e) {
    map.flyTo([e.lat, e.lon], Math.max(map.getZoom(), 9), { duration: 0.6 });
    eventsLayer.eachLayer((l) => { const p = l.getLatLng(); if (Math.abs(p.lat - e.lat) < 1e-6 && Math.abs(p.lng - e.lon) < 1e-6) l.openPopup(); });
    if (isPhone()) $("sheet").classList.remove("expanded");
  }

  /* ---------- analyst ---------- */
  const REG_COLS = [["name", "col_region"], ["avg", "col_avg"], ["trend", "col_trend"], ["last", "col_last"], ["large", "col_large"], ["risk", "col_risk"]];
  function renderAnalyst() {
    const rows = S.regions.filter(inArea).map((r) => ({ id: r.id, name: regionName(r.id), avg: r.avg_per_year, trend: r.trend_pct_decade,
      last: r.last_month ? r.last_month.anomaly_pct : null, large: r.events_recent ? r.events_recent.large : 0, risk: S.risk.regions[r.id] ? S.risk.regions[r.id].prob : null }));
    const { key, asc } = S.regSort;
    rows.sort((a, b) => (key === "name" ? a.name.localeCompare(b.name) : (a[key] ?? -1e9) - (b[key] ?? -1e9)) * (asc ? 1 : -1));
    const nR = S.open.reg ? rows.length : Math.min(10, rows.length);
    const cls = (v) => (v == null ? "" : v > 0 ? "num-up" : v < 0 ? "num-down" : "");
    $("reg-table").innerHTML = `<thead><tr>${REG_COLS.map(([k, l]) => `<th data-k="${k}" class="${key === k ? `sorted${asc ? " asc" : ""}` : ""}">${esc(t(l))}</th>`).join("")}</tr></thead>
      <tbody>${rows.slice(0, nR).map((r) => `<tr data-id="${esc(r.id)}" class="${S.area.region === r.id ? "sel" : ""}"><td>${esc(r.name)}</td><td>${fmt(r.avg)}</td>
        <td class="${cls(r.trend)}">${r.trend == null ? "–" : `${signed(r.trend)}%`}</td><td class="${cls(r.last)}">${r.last == null ? "–" : `${signed(r.last)}%`}</td>
        <td>${fmt(r.large)}</td><td>${r.risk == null ? "–" : pct(r.risk)}</td></tr>`).join("")}</tbody>`;
    moreButton($("reg-more"), rows.length, nR, "reg");
    const ev = S.events;
    $("ev-types").innerHTML = ["large", "recurrent", "small"].map((tp) => `<button type="button" class="chip${S.evTypes.has(tp) ? " active" : ""}" data-t="${tp}"><i style="background:${TYPE_COLOR[tp]}"></i>${esc(t(`type_${tp}`))} <small>${fmt(ev.by_type[tp] || 0)}</small></button>`).join("");
    $("ev-min").value = String(S.evMin); $("ev-sort").value = S.evSort;
    $("ev-count").textContent = t("ev_count", { n: fmt(ev.total), a: fmt(ev.area_km2_total) });
    const nE = Math.min(S.open.ev, ev.events.length);
    $("ev-list").innerHTML = ev.events.slice(0, nE).map(eventRow).join("");
    moreButton($("ev-more"), ev.events.length, nE, "ev", 15);
    renderEvYear(ev); renderEvScatter();
    drawEvents(ev.events.slice(0, 60));
  }
  function renderEvYear(ev) {
    const years = [...new Set(ev.by_year.map((r) => r.year))].sort(), ax = axis();
    CH["ev-year-chart"].setOption({
      ...base(), grid: { left: 42, right: 8, top: 52, bottom: 22 }, legend: legend(),
      tooltip: { ...tooltip(), trigger: "axis", axisPointer: { type: "line", lineStyle: { color: C.line2 } }, valueFormatter: (x) => fmt(x) },
      xAxis: { type: "category", data: years, ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, interval: (i) => i % 3 === 0 } },
      yAxis: { type: "value", ...ax, axisLabel: { ...ax.axisLabel, formatter: kfmt } },
      series: ["small", "recurrent", "large"].filter((tp) => S.evTypes.has(tp)).map((tp) => ({ name: t(`type_${tp}`), type: "bar", stack: "n", color: TYPE_COLOR[tp], barMaxWidth: 14,
        data: years.map((y) => { const r = ev.by_year.find((x) => x.year === y && x.type === tp); return r ? r.n : 0; }) })),
    }, true);
  }
  function renderEvScatter() {
    const ax = axis(), data = S.evScatter || [];
    CH["ev-scatter"].setOption({
      ...base(), grid: { left: 46, right: 10, top: 82, bottom: 34 }, legend: legend(),
      tooltip: { ...tooltip(), formatter: (p) => eventPopup(p.data[2]) },
      xAxis: { type: "log", name: t("c_area"), nameLocation: "middle", nameGap: 22, nameTextStyle: { color: C.muted, fontSize: 10.5, fontFamily: C.mono }, ...ax, min: 0.1 },
      yAxis: { type: "log", name: t("c_frp"), nameGap: 10, nameTextStyle: { color: C.muted, fontSize: 10.5, fontFamily: C.mono, align: "left" }, ...ax, min: 1 },
      series: ["small", "recurrent", "large"].map((tp) => ({ name: t(`type_${tp}`), type: "scatter", color: TYPE_COLOR[tp], symbol: "rect", symbolSize: 5, itemStyle: { opacity: 0.8 },
        data: data.filter((e) => e.type === tp && e.frp_max_mw > 0).map((e) => [e.area_km2, e.frp_max_mw, e]) })),
    }, true);
  }

  /* ---------- legend on the map ---------- */
  function renderLegend() {
    const v = S.view, m = viewMetric();
    $("metric-wrap").hidden = v !== "analyst";
    $("metric").value = S.metric;
    const sw = v !== "overview" && m === "risk";
    $("swatches").hidden = !sw; $("ramp").hidden = sw; $("ramp-labels").hidden = sw;
    if (v === "overview") {
      $("legend-label").textContent = S.mapMonth ? t("legend_month", { m: MI(S.mapMonth.m - 1), y: S.mapMonth.y }) : S.mapYear ? t("legend_year", { y: S.mapYear }) : t("legend_avg");
      $("ramp").style.background = `linear-gradient(90deg, ${C.ramp.join(", ")})`;
      $("ramp-labels").innerHTML = "<span>1</span><span>10</span><span>100</span><span>1000+</span>";
      return;
    }
    $("legend-label").textContent = t(`metric_${m}`);
    if (v === "emergency" && S.emMode === "fires") {
      $("legend-label").textContent = t("fires_legend");
      $("swatches").innerHTML = ["large", "recurrent", "small"].map((k) => `<span><i style="background:${TYPE_COLOR[k]}"></i>${esc(t(`type_${k}`))}</span>`).join("");
      return;
    }
    if (v === "emergency" && cellsShown()) {
      $("legend-label").textContent = t("cell_legend");
      $("swatches").innerHTML = [["low", "<10%"], ["elevated", "10–25%"], ["high", "25–50%"], ["very_high", "≥50%"]]
        .map(([l, r]) => `<span><i style="background:${C.lvl[l]}"></i>${esc(t(`lvl_${l}`))}<small>${r}</small></span>`).join("");
      return;
    }
    if (sw) {
      $("swatches").innerHTML = [["low", "<20%"], ["elevated", "20–40%"], ["high", "40–60%"], ["very_high", "≥60%"]]
        .map(([l, r]) => `<span><i style="background:${C.lvl[l]}"></i>${esc(t(`lvl_${l}`))}<small>${r}</small></span>`).join("");
    } else if (m === "trend" || m === "last") {
      $("ramp").style.background = `linear-gradient(90deg, ${DIVERGE.join(", ")})`;
      $("ramp-labels").innerHTML = m === "trend" ? "<span>−80%</span><span>0</span><span>+80%</span>" : "<span>−75%</span><span>0</span><span>+300%</span>";
    } else {
      $("ramp").style.background = `linear-gradient(90deg, ${C.ramp.join(", ")})`;
      $("ramp-labels").innerHTML = `<span>0</span><span>${fmt(Math.max(...S.regions.map((r) => metricValue(r, m) || 0)))}</span>`;
    }
  }

  /* ---------- method ---------- */
  function renderMethod() {
    const v = S.meta.validation, u = S.meta.uncertainty;
    const cell = (k, lvl) => `<td>${fmt(v[lvl][k].r2_log, 2)}</td><td>${signed(v[lvl][k].bias_pct)}%</td>`;
    $("val-table").innerHTML = `<table class="val"><thead><tr><th>${t("v_level")}</th><th colspan="2">${t("v_region")}</th><th colspan="2">${t("v_block")}</th></tr>
      <tr><th></th><th>${t("v_r2")}</th><th>${t("v_bias")}</th><th>${t("v_r2")}</th><th>${t("v_bias")}</th></tr></thead><tbody>
      ${[["raw", "v_raw"], ["const", "v_const"], ["ours", "v_ours"]].map(([k, n]) => `<tr class="${k === "ours" ? "ours" : ""}"><td>${t(n)}</td>${cell(k, "region_monthly")}${cell(k, "block_1deg_monthly")}</tr>`).join("")}</tbody></table>`;
    const cov = u.coverage_90_on_test;
    $("val-cov").textContent = t("v_cov", { r: fmt(cov.region_monthly * 100), b: fmt(cov.block_1deg_monthly * 100) });
    if (S.meta.risk && S.meta.risk.available) {
      const rv = S.meta.risk.validation, a = rv.ablation;
      $("risk-val").innerHTML = `<table class="val"><thead><tr><th>${t("rv_model")}</th><th>${t("rv_auc")}</th></tr></thead><tbody>
        ${[["rv_clim", { auc: rv.auc_climatology }], ["rv_season", a.season_only], ["rv_weather", a.season_weather], ["rv_fires", a.season_fires], ...(a.v1 ? [["rv_v1", a.v1]] : []), ["rv_all", a.all]]
          .map(([k, x]) => `<tr class="${k === "rv_all" ? "ours" : ""}"><td>${t(k)}</td><td>${fmt(x.auc, 3)}</td></tr>`).join("")}</tbody></table>
        ${rv.importance_auc_drop ? `<h3 style="margin-top:12px">${esc(t("imp_title"))}</h3><table class="val"><tbody>${Object.entries(rv.importance_auc_drop)
          .map(([g, d]) => `<tr><td>${esc(t(`grp_${g}`))}</td><td>−${fmt(d, 3)}</td></tr>`).join("")}</tbody></table>` : ""}
        <p class="note">${esc(t("rv_note"))}</p>`;
    }
  }
  let ratioChart = null;
  function renderRatio() {
    if (!S.meta) return;
    if (!ratioChart) ratioChart = echarts.init($("ratio-chart"));
    const ar = S.meta.annual_ratio || {}, yrs = Object.keys(ar).filter((y) => +y <= S.meta.last_full_year), ax = axis();
    ratioChart.setOption({ ...base(), grid: { left: 30, right: 4, top: 8, bottom: 20 }, tooltip: { ...tooltip(), trigger: "axis", valueFormatter: (x) => fmt(x, 2) },
      xAxis: { type: "category", data: yrs, ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, interval: (i) => i % 3 === 0 } },
      yAxis: { type: "value", min: 2.5, ...ax },
      series: [{ type: "bar", barWidth: "55%", data: yrs.map((y) => ({ value: ar[y], itemStyle: { color: +y <= 2021 ? C.ok : C.accent } })) }] }, true);
    setTimeout(() => ratioChart.resize(), 0);
  }

  /* ---------------- time-lapse ---------------- */
  async function play() {
    if (S.playing) { stopPlay(); return; }
    S.playing = true; S.mapMonth = null; $("clear-month").hidden = true;
    $("play").setAttribute("aria-pressed", "true"); $("play-label").textContent = t("pause");
    const years = S.meta.years_full;
    let i = S.mapYear ? Math.max(0, years.indexOf(+S.mapYear) + 1) : 0;
    if (i >= years.length) i = 0;
    while (S.playing && i < years.length) {
      S.mapYear = String(years[i]); $("map-year").value = S.mapYear;
      getGrid(years[i + 1]).catch(() => {});
      await showGrid();
      await new Promise((r) => setTimeout(r, 750));
      i++;
    }
    stopPlay();
  }
  function stopPlay() { S.playing = false; $("play").setAttribute("aria-pressed", "false"); $("play-label").textContent = t("play"); }

  /* ---------------- downloads ---------------- */
  function save(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  const slug = () => (S.labelFn ? S.labelFn() : "area").toLowerCase().replace(/[^a-z0-9а-яёәіңғүұқөһ]+/gi, "_").replace(/^_|_$/g, "").slice(0, 40);
  async function withBusy(btn, fn) {
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = t("dl_preparing"); $("dl-msg").hidden = true;
    try { await fn(); } catch (e) { $("dl-msg").hidden = false; $("dl-msg").textContent = e.status === 413 ? t("dl_toolarge") : `${t("error")} ${e.message}`; }
    finally { btn.disabled = false; btn.textContent = label; }
  }
  $("dl-monthly").onclick = (e) => withBusy(e.currentTarget, async () => save(await api("/api/calendar.csv", periodBody(), "blob"), `fire_calendar_${slug()}.csv`));
  $("dl-json").onclick = () => save(new Blob([JSON.stringify({ area: S.area, label: S.labelFn && S.labelFn(), analytics: S.an }, null, 2)], { type: "application/json" }), `fire_analytics_${slug()}.json`);
  $("dl-points").onclick = (e) => withBusy(e.currentTarget, async () => save(await api("/api/hotspots.csv", { ...S.area, start: $("dl-start").value || null, end: $("dl-end").value || null }, "blob"), `hotspots_${slug()}.csv`));
  $("dl-events").onclick = () => {
    if (!S.events) return;
    const cols = ["id", "start", "end", "duration_days", "detections", "area_km2", "frp_max_mw", "frp_sum_mw", "night_share", "extent_km", "spread_km_day", "recurrence", "type", "intense", "lat", "lon", "region", "country"];
    save(new Blob([[cols.join(",")].concat(S.events.events.map((e) => cols.map((c) => (e[c] == null ? "" : String(e[c]))).join(","))).join("\n")], { type: "text/csv" }), `fire_events_${slug()}.csv`);
  };

  /* ---------------- view, language, theme ---------------- */
  function setView(v, { reload = true } = {}) {
    S.view = v; store.set("fc-view", v);
    pickerSummary();
    document.body.classList.remove("view-overview", "view-emergency", "view-analyst");
    document.body.classList.add(`view-${v}`);
    document.querySelectorAll("#views button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === v)));
    stopPlay(); tip.hidden = true;
    if (heatLayer) { if (v === "overview") { heatLayer.addTo(map); heatLayer.bringToBack(); } else map.removeLayer(heatLayer); }
    eventsLayer.clearLayers(); cellsLayer.clearLayers(); $("risk-views").hidden = true;
    renderTabs();
    renderLegend();
    restyleMap();
    if (reload) load();
  }
  function applyStatic() {
    document.documentElement.lang = S.lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); el.setAttribute("aria-label", t(el.dataset.i18nTitle)); });
    document.querySelectorAll(".lang button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === S.lang)));
    $("radius-out").textContent = `${S.radius} ${t("km")}`;
    $("play-label").textContent = S.playing ? t("pause") : t("play");
    if (S.meta) {
      $("data-date").textContent = t("data_through", { d: dateFmt(S.meta.date_range[1]) });
      const cur = $("country").value;
      $("country").innerHTML = `<option value="">${esc(t("all_ca"))}</option>` + S.meta.countries.map((c) => `<option value="${esc(c)}">${esc(countryName(c))}</option>`).join("");
      $("country").value = cur;
      fillRegionSelect();
      const my = $("map-year").value;
      $("map-year").innerHTML = `<option value="">${esc(t("map_avg"))}</option>` + S.meta.years_full.map((y) => `<option>${y}</option>`).join("");
      $("map-year").value = my;
    }
    $("places").innerHTML = PLACES.map((p, i) => `<button type="button" class="chip${S.place === p ? " active" : ""}" data-i="${i}">${esc(p[S.lang])}</button>`).join("");
    renderTabs();
  }
  function rerender() { if (S.meta) renderMethod(); if ((S.view === "analyst" && S.events) || (S.view !== "analyst" && S.cal)) render(); if (S.tab[S.view] === "data") renderRatio(); }
  function setLang(l) { S.lang = l; store.set("fc-lang", l); applyStatic(); rerender(); document.dispatchEvent(new CustomEvent("fc:lang")); }
  function setTheme(th) {
    S.theme = th; document.documentElement.dataset.theme = th; store.set("fc-theme", th);
    readColors(); setTiles();
    document.dispatchEvent(new CustomEvent("fc:theme"));
    if (S.grid) drawHeat(S.grid);
    areaLayer.eachLayer((l) => l.setStyle && l.setStyle({ color: C.accent }));
    restyleMap(); renderLegend(); rerender();
  }

  /* ---------------- sheet + guide ---------------- */
  function showSheet(on) { document.body.classList.toggle("sheet-hidden", !on); $("sheet-show").hidden = on; if (on) setTimeout(resizeCharts, 250); }
  function sheetMin(on) {
    document.body.classList.toggle("sheet-min", on);
    if (on) $("sheet").classList.remove("expanded");
    $("sheet-toggle").setAttribute("aria-expanded", String(!on && $("sheet").classList.contains("expanded")));
    setTimeout(() => { map.invalidateSize(); resizeCharts(); refit(); }, 280);
  }
  /* phone: after a panel folds or opens, fit the selected area into the space that is really free */
  function refit() {
    if (!isPhone()) return;
    const g = selectedGeometry();
    if (g) fitPadded(L.geoJSON(g).getBounds());
  }
  $("sheet-hide").onclick = () => (isPhone() ? sheetMin(!document.body.classList.contains("sheet-min")) : showSheet(false));
  $("sheet-show").onclick = () => showSheet(true);
  $("sheet-toggle").onclick = () => {
    if (document.body.classList.contains("sheet-min")) { sheetMin(false); return; }
    const ex = $("sheet").classList.toggle("expanded"); $("sheet-toggle").setAttribute("aria-expanded", String(ex)); setTimeout(resizeCharts, 250);
  };
  $("area-name").addEventListener("click", () => { if (isPhone() && document.body.classList.contains("sheet-min")) sheetMin(false); });
  /* legend: compact on phones, the label opens the scale */
  $("legend-label").addEventListener("click", () => { if (isPhone()) document.body.classList.toggle("legend-min"); });
  function guide(open) { $("onb").hidden = !open; if (open) $("onb-ok").focus(); else store.set("fc-onb", "1"); }
  $("help-btn").onclick = () => guide(true);
  $("onb-ok").onclick = () => guide(false);
  $("onb").onclick = (e) => { if (e.target === $("onb")) guide(false); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("onb").hidden) guide(false); });

  /* ---------------- wiring ---------------- */
  document.querySelectorAll(".lang button").forEach((b) => { b.onclick = () => setLang(b.dataset.lang); });
  $("theme-btn").onclick = () => setTheme(S.theme === "dark" ? "light" : "dark");
  document.querySelectorAll("#views button").forEach((b) => { b.onclick = () => setView(b.dataset.view); });
  $("country").onchange = () => selectCountry($("country").value);
  $("region").onchange = () => { const v = $("region").value; if (v) selectRegion(v); else selectCountry($("country").value); };
  $("draw-rect").onclick = () => startDraw("rect");
  $("draw-poly").onclick = () => startDraw("poly");
  $("point-btn").onclick = () => { stopDraw(); setPointMode(S.mode !== "point"); };
  $("places").onclick = (e) => {
    const b = e.target.closest(".chip"); if (!b) return;
    const p = PLACES[+b.dataset.i];
    S.radius = p.r; $("radius").value = p.r; $("radius-out").textContent = `${p.r} ${t("km")}`;
    setPoint(p.lat, p.lon, p);
    fitPadded(L.latLng(p.lat, p.lon).toBounds(p.r * 2400));
  };
  let radiusTimer = null;
  $("radius").oninput = (e) => { S.radius = +e.target.value; $("radius-out").textContent = `${S.radius} ${t("km")}`; clearTimeout(radiusTimer); radiusTimer = setTimeout(applyPoint, 350); };
  $("map-year").onchange = (e) => { stopPlay(); S.mapMonth = null; $("clear-month").hidden = true; S.mapYear = e.target.value; showGrid(); };
  $("play").onclick = play;
  $("back-now").onclick = () => { stopPlay(); S.mapYear = ""; S.mapMonth = null; $("map-year").value = ""; $("clear-month").hidden = true; showGrid(); };
  $("clear-month").onclick = () => { S.mapMonth = null; $("clear-month").hidden = true; showGrid(); };
  $("show-raw").onchange = (e) => { S.showRaw = e.target.checked; renderTrend(); };
  document.querySelectorAll("#cal-modes button").forEach((b) => { b.onclick = () => { S.calMode = b.dataset.cal; renderCalendar(); }; });
  $("retry").onclick = () => load();
  function onPeriod() {
    let a = +$("from").value, b = +$("to").value;
    if (a > b) { [a, b] = [b, a]; $("from").value = a; $("to").value = b; }
    S.from = a; S.to = b; load();
  }
  $("from").onchange = onPeriod; $("to").onchange = onPeriod;
  $("metric").onchange = (e) => { S.metric = e.target.value; renderLegend(); restyleMap(); };
  $("panels").addEventListener("click", (e) => {
    const more = e.target.closest(".more-btn");
    if (more) { const k = more.dataset.k; if (k === "ev") S.open.ev += 15; else S.open[k] = !S.open[k]; render(); return; }
    const th = e.target.closest("#reg-table th[data-k]");
    if (th) { const k = th.dataset.k; S.regSort = { key: k, asc: S.regSort.key === k ? !S.regSort.asc : k === "name" }; renderAnalyst(); return; }
    const tr = e.target.closest("#reg-table tr[data-id]");
    if (tr) { selectRegion(tr.dataset.id); return; }
    const rl = e.target.closest("#risk-list li[data-id], #risk-head .risk-card.pick[data-id]");
    if (rl && rl.dataset.id !== S.area.region) { selectRegion(rl.dataset.id); return; }
    const pl = e.target.closest(".places .row[data-cell]");
    if (pl && S.cells) { const c = S.cells.cells[+pl.dataset.cell], d = S.cells.cell_deg; map.flyToBounds([[c.lat, c.lon], [c.lat + d, c.lon + d]], { maxZoom: 9, duration: 0.6 }); return; }
    const ar = e.target.closest("#active-list .row[data-i]");
    if (ar) { flyToEvent(S.active.events[+ar.dataset.i]); return; }
    const er = e.target.closest("#ev-list .row[data-i]");
    if (er) { flyToEvent(S.events.events[+er.dataset.i]); return; }
    const chip = e.target.closest("#ev-types .chip[data-t]");
    if (chip) { const tp = chip.dataset.t; if (S.evTypes.has(tp) && S.evTypes.size > 1) S.evTypes.delete(tp); else S.evTypes.add(tp); load(); }
  });
  $("ev-min").onchange = (e) => { S.evMin = +e.target.value; load(); };
  $("ev-sort").onchange = (e) => { S.evSort = e.target.value; load(); };

  function applyHash() {
    const h = decodeURIComponent(location.hash.slice(1));
    if (h.startsWith("c=") && S.meta.countries.includes(h.slice(2))) { selectCountry(h.slice(2)); return true; }
    if (h.startsWith("r=") && S.regionsById[h.slice(2)]) { selectRegion(h.slice(2)); return true; }
    if (h.startsWith("p=")) {
      const [lat, lon, r] = h.slice(2).split(",").map(Number);
      if ([lat, lon, r].every(Number.isFinite) && r > 0 && r <= 200) {
        $("more").open = true; setPointMode(true);
        S.radius = r; $("radius").value = r; $("radius-out").textContent = `${r} ${t("km")}`;
        setPoint(lat, lon, PLACES.find((p) => p.lat === lat && p.lon === lon) || null);
        fitPadded(L.latLng(lat, lon).toBounds(r * 2400));
        return true;
      }
    }
    return false;
  }

  /* small bridge for the assistant widget (web/assistant.js) */
  window.FC = {
    S, t, fmt, esc, hs, plural, regionName, countryName, dateFmt, dRange, map, api, getJSON, selectRegion, selectCountry,
    setView: (v) => { if (v !== S.view) setView(v); },
    showPoint(lat, lon, r = 25) {
      $("more").open = true; stopDraw(); setPointMode(true);
      S.radius = Math.max(10, Math.min(200, Math.round(r / 5) * 5)); $("radius").value = S.radius; $("radius-out").textContent = `${S.radius} ${t("km")}`;
      setPoint(lat, lon, null);
      fitPadded(L.latLng(lat, lon).toBounds(S.radius * 2400));
    },
    label: () => (S.labelFn ? S.labelFn() : t("all_ca")),
    /* centre of the current selection (null for the whole of Central Asia) */
    areaCenter() {
      if (S.area.lat != null) return { lat: S.area.lat, lon: S.area.lon };
      const g = selectedGeometry();
      if (!g) return null;
      const c = L.geoJSON(g).getBounds().getCenter();
      return { lat: c.lat, lon: c.lng };
    },
    /* open the tab that holds a report block, unfold it and scroll to it */
    showBlock(id) {
      const sec = $(id);
      if (!sec) return;
      const keys = sec.dataset.panel.split(" ");
      const key = keys.find((k) => k.startsWith(`${S.view}:`)) || keys[0];
      const [v, tb] = key.split(":");
      if (v !== S.view) setView(v);
      S.tab[v] = tb; renderTabs();
      if (sec.classList.contains("folded")) { const b = sec.querySelector(".fold-btn"); if (b) b.click(); }
      showSheet(true); $("sheet").classList.add("expanded");
      setTimeout(() => sec.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
    },
  };

  /* the same page serves the Central Asia and the United States sites: names, quick places, map view, links */
  function applyDomain(d) {
    S.domain = d;
    for (const [lang, o] of Object.entries(d.i18n || {})) Object.assign(I18N[lang] || {}, o);
    if (d.places) window.PLACES = d.places;
    if (d.name) document.title = `FireCalendar · ${d.name.en}`;
    document.body.classList.toggle("no-alerts", d.alerts === false);
    document.body.dataset.domain = d.id || "ca";
    document.querySelectorAll("#dom-switch a").forEach((a) => {
      const k = a.dataset.dom;
      a.href = (d.sites && d.sites[k]) || "#";
      a.classList.toggle("on", k === (d.id || "ca"));
      if (k === (d.id || "ca")) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
  }

  (async function init() {
    readColors(); setTiles();
    document.body.classList.add(`view-${S.view}`);
    if (isPhone()) { foldPicker(true); document.body.classList.add("legend-min"); }
    applyStatic();
    if (store.get("fc-onb") !== "1") guide(true);
    status("loading");
    try {
      const [meta, regs, risk] = await Promise.all([getJSON("/api/meta"), getJSON("/api/regions"), getJSON("/api/risk").catch(() => null)]);
      applyDomain(meta.domain || {});
      S.meta = meta; S.regions = regs.regions; S.regionsById = Object.fromEntries(regs.regions.map((r) => [r.id, r])); S.risk = risk;
      await loadGeo();
    } catch (e) {
      status("error", e.message); $("retry").onclick = () => location.reload(); return;
    }
    const m = S.meta, first = m.date_range[0], last = m.date_range[1];
    const firstFull = first.endsWith("-01-01") ? +first.slice(0, 4) : +first.slice(0, 4) + 1;
    m.last_year = +last.slice(0, 4);
    m.last_full_year = last.endsWith("-12-31") ? m.last_year : m.last_year - 1;
    m.years_full = Array.from({ length: m.last_year - firstFull + 1 }, (_, i) => firstFull + i);
    const yo = m.years_full.map((y) => `<option>${y}</option>`).join("");
    $("from").innerHTML = yo; $("to").innerHTML = yo;
    S.from = firstFull; S.to = m.last_year; $("from").value = S.from; $("to").value = S.to;
    const lastD = new Date(`${last}T00:00:00Z`);
    $("dl-end").value = last; $("dl-end").max = last; $("dl-start").min = first; $("dl-end").min = first;
    $("dl-start").value = new Date(Date.UTC(lastD.getUTCFullYear(), lastD.getUTCMonth() - 1, 1)).toISOString().slice(0, 10);
    applyStatic(); renderMethod();
    const b = (S.domain && S.domain.view) || m.bounds;
    map.fitBounds([[b.south, b.west], [b.north, b.east]], isPhone() ? { paddingTopLeft: [8, 230], paddingBottomRight: [8, window.innerHeight * 0.48] }
      : { paddingTopLeft: [360, 80], paddingBottomRight: [510, 70] });
    showGrid();
    setView(S.view, { reload: false });
    if (!applyHash()) selectCountry("", { fit: false });
    document.dispatchEvent(new CustomEvent("fc:ready"));
    window.addEventListener("hashchange", () => {
      const h = location.hash.slice(1);
      const cur = S.area.country ? `c=${encodeURIComponent(S.area.country)}` : S.area.region ? `r=${encodeURIComponent(S.area.region)}` : S.area.lat != null ? `p=${S.area.lat},${S.area.lon},${S.area.radius_km}` : "";
      if (h !== cur && !applyHash()) selectCountry("");
    });
  })();
})();
