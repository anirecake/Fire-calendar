/* FireCalendar AI assistant widget.
   Online: questions go to /api/assistant (Claude + our data and risk model as tools); the answer may
   move the map (actions). Offline (no API key on the server, or it is unreachable): the same quick
   questions are answered from data the page already loaded. */
(() => {
  "use strict";
  const FC = window.FC;
  if (!FC) return;
  const $ = (id) => document.getElementById(id);
  const t = (k, v) => FC.t(k, v);
  const st = { open: false, llm: null, msgs: [], busy: false, loc: null, bot: null };

  /* ---------- DOM ---------- */
  const fab = document.createElement("button");
  fab.type = "button"; fab.id = "ai-fab"; fab.className = "ai-fab";
  fab.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg><span class="ai-fab-l" data-ai="ai_fab"></span>`;
  const panel = document.createElement("section");
  panel.id = "ai-panel"; panel.className = "ai-panel panel-box"; panel.hidden = true;
  panel.setAttribute("role", "dialog"); panel.setAttribute("aria-labelledby", "ai-title");
  panel.innerHTML = `
    <header class="ai-head">
      <div><h2 id="ai-title" data-ai="ai_title"></h2><span class="ai-mode" id="ai-mode"></span></div>
      <button type="button" class="icon-btn" id="ai-close" aria-label="close"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </header>
    <div class="ai-log" id="ai-log" aria-live="polite"></div>
    <div class="chips ai-chips" id="ai-chips"></div>
    <form class="ai-form" id="ai-form">
      <textarea id="ai-input" rows="1" maxlength="1200" data-ai-ph="ai_placeholder"></textarea>
      <button type="submit" class="btn btn-primary" id="ai-send" aria-label="send"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l16-8-6 16-2.5-6.5z"/></svg></button>
    </form>
    <p class="ai-foot" data-ai="ai_disclaimer"></p>`;
  document.body.append(fab, panel);

  const CHIPS = ["risk", "near", "area", "how", "model", "tg"];
  function translate() {
    document.querySelectorAll("[data-ai]").forEach((el) => { el.textContent = t(el.dataset.ai); });
    $("ai-input").placeholder = t("ai_placeholder");
    $("ai-close").setAttribute("aria-label", t("ai_close"));
    fab.setAttribute("aria-label", t("ai_title"));
    $("ai-mode").textContent = st.llm == null ? "" : st.llm ? t("ai_mode_llm") : t("ai_mode_off");
    $("ai-mode").className = `ai-mode ${st.llm ? "on" : "off"}`;
    $("ai-chips").innerHTML = CHIPS
      .map((c) => `<button type="button" class="chip" data-q="${c}">${FC.esc(t(`ai_q_${c}`))}</button>`).join("");
    const link = $("tg-link");
    if (link && st.bot) { link.href = `https://t.me/${st.bot}`; link.textContent = `@${st.bot}`; }
  }
  document.addEventListener("fc:lang", () => { translate(); if (!st.msgs.length && st.open) greet(); });

  /* ---------- rendering ---------- */
  function md(s) {        // tiny, safe markdown: escape first, then **bold**, bullets, line breaks, our own links
    let h = FC.esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    h = h.replace(/\[(.+?)\]\((https:\/\/t\.me\/[A-Za-z0-9_]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const lines = h.split(/\n/);
    let out = "", list = false;
    for (const ln of lines) {
      const m = ln.match(/^\s*(?:[-•*]|\d+[.)])\s+(.*)/);
      if (m) { if (!list) { out += "<ul>"; list = true; } out += `<li>${m[1]}</li>`; continue; }
      if (list) { out += "</ul>"; list = false; }
      out += ln.trim() ? `<p>${ln}</p>` : "";
    }
    return out + (list ? "</ul>" : "");
  }
  function bubble(role, text, meta) {
    const d = document.createElement("div");
    d.className = `ai-msg ${role}`;
    d.innerHTML = role === "user" ? `<p>${FC.esc(text)}</p>` : md(text);
    if (meta) { const m = document.createElement("p"); m.className = "ai-meta"; m.textContent = meta; d.appendChild(m); }
    $("ai-log").appendChild(d);
    $("ai-log").scrollTop = $("ai-log").scrollHeight;
    return d;
  }
  function greet() { $("ai-log").innerHTML = ""; bubble("bot", st.llm ? t("ai_hello_llm") : t("ai_hello_off")); }

  /* ---------- open / close ---------- */
  async function open(on) {
    st.open = on; panel.hidden = !on; fab.setAttribute("aria-expanded", String(on)); fab.classList.toggle("active", on);
    document.body.classList.toggle("ai-open", on);
    if (!on) { fab.focus(); return; }
    if (st.llm == null) {
      const s = await FC.getJSON("/api/assistant/status").catch(() => ({ llm: false }));
      st.llm = !!s.llm; translate();
    }
    if (!st.msgs.length) greet();
    setTimeout(() => $("ai-input").focus(), 50);
  }
  fab.onclick = () => open(!st.open);
  $("ai-close").onclick = () => open(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && st.open) open(false); });

  /* ---------- asking ---------- */
  const context = () => ({ view: FC.S.view, area: FC.S.area.geometry ? { drawn: true } : FC.S.area, area_label: FC.label(), location: st.loc });
  function locate() {
    return new Promise((res) => {
      if (st.loc) return res(st.loc);
      if (!navigator.geolocation) return res(null);
      navigator.geolocation.getCurrentPosition((p) => { st.loc = { lat: p.coords.latitude, lon: p.coords.longitude }; res(st.loc); },
        () => res(null), { timeout: 8000, maximumAge: 600000 });
    });
  }
  function doActions(acts) {
    for (const a of acts || []) {
      if (a.view) FC.setView(a.view);
      if (a.region_id) FC.selectRegion(a.region_id);
      else if (a.country) FC.selectCountry(a.country);
      else if (a.lat != null && a.lon != null) FC.showPoint(a.lat, a.lon, a.radius_km || 25);
    }
  }
  async function ask(text, intent) {
    if (st.busy || !text.trim()) return;
    st.busy = true; $("ai-send").disabled = true;
    bubble("user", text);
    st.msgs.push({ role: "user", content: text });
    const wait = bubble("bot typing", t("ai_thinking"));
    let reply = null, meta = null;
    try {
      if (intent === "near") await locate();
      if (st.llm && intent !== "tg") {
        try {
          const r = await fetch("/api/assistant", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ messages: st.msgs.slice(-12), lang: FC.S.lang, context: context() }) });
          if (r.status === 429) reply = t("ai_busy");
          else if (!r.ok) throw new Error(`HTTP ${r.status}`);
          else {
            const j = await r.json();
            reply = j.reply || (j.refused ? t("ai_refused") : t("ai_err"));
            if (j.tools_used && j.tools_used.length) meta = `${t("ai_used")}: ${[...new Set(j.tools_used)].map((x) => t(`ai_tool_${x}`)).join(", ")}`;
            doActions(j.actions);
          }
        } catch (e) {
          reply = await offline(intent || guess(text));
          meta = t("ai_fallback_note");
        }
      } else {
        reply = await offline(intent || guess(text));
      }
    } catch (e) { reply = t("ai_err"); }
    wait.remove();
    bubble("bot", reply, meta);
    st.msgs.push({ role: "assistant", content: reply });
    st.busy = false; $("ai-send").disabled = false;
  }
  $("ai-form").onsubmit = (e) => { e.preventDefault(); const v = $("ai-input").value; $("ai-input").value = ""; autosize(); ask(v); };
  $("ai-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ai-form").requestSubmit(); } });
  const autosize = () => { const a = $("ai-input"); a.style.height = "auto"; a.style.height = `${Math.min(a.scrollHeight, 110)}px`; };
  $("ai-input").addEventListener("input", autosize);
  $("ai-chips").onclick = (e) => { const b = e.target.closest(".chip[data-q]"); if (b) ask(b.textContent, b.dataset.q); };

  /* ---------- offline answers (from data the page already has) ---------- */
  function guess(q) {
    q = q.toLowerCase();
    if (/telegram|телеграм|бот|уведом|оповещ|хабар/.test(q)) return "tg";
    if (/рядом|near|около|жақын|маңында|возле|my location|мен тұрған/.test(q)) return "near";
    if (/риск|risk|қауіп|прогноз|forecast|болжам/.test(q)) return /как|how|қалай|работ|точн|accura|дәл/.test(q) ? "model" : "risk";
    if (/пользоват|how to|қалай пайдалан|how do i|как скачать|download|жүктеп/.test(q)) return "how";
    if (/област|регион|region|облыс|здесь|here|мұнда/.test(q)) return "area";
    return "help";
  }
  async function offline(intent) {
    const S = FC.S;
    if (intent === "risk") {
      if (!S.risk) return t("ai_err");
      const top = Object.entries(S.risk.regions).sort((a, b) => b[1].prob - a[1].prob).slice(0, 5);
      doActions([{ view: "emergency" }]);
      return `${t("ai_risk_intro", { d: FC.dateFmt(S.risk.issue_date) })}\n` +
        top.map(([id, r]) => `- **${FC.regionName(id)}**: ${Math.round(r.prob * 100)}% (${t(`lvl_${r.level}`)})`).join("\n");
    }
    if (intent === "near") {
      const loc = await locate();
      if (!loc) return t("ai_geo_denied");
      const last = new Date(`${S.meta.date_range[1]}T00:00:00Z`);
      const since = new Date(last.getTime() - 7 * 864e5).toISOString().slice(0, 10);
      const r = await FC.api("/api/events", { lat: loc.lat, lon: loc.lon, radius_km: 50, since, sort: "start", limit: 5 });
      FC.showPoint(loc.lat, loc.lon, 50);
      if (!r.total) return t("ai_near_none", { d: FC.dateFmt(S.meta.date_range[1]) });
      const near = r.events.map((e) => ({ ...e, km: dist(loc, e) })).sort((a, b) => a.km - b.km)[0];
      return t("ai_near_some", { n: r.total, km: FC.fmt(near.km), d: FC.dateFmt(near.end), a: FC.fmt(near.area_km2, 1) });
    }
    if (intent === "area") {
      const main = $("ans-main") ? $("ans-main").textContent : "";
      const txt = (id) => ($(id) ? [...$(id).childNodes].map((n) => n.textContent.trim()).filter(Boolean).join(" · ") : "");
      const ytd = txt("f-ytd"), season = txt("f-season");
      return `**${FC.label()}**\n${main}\n- ${t("fact_ytd")}: ${ytd}\n- ${t("fact_season")}: ${season}`;
    }
    if (intent === "how") return t("ai_how_text");
    if (intent === "model") {
      const v = S.risk && S.risk.validation;
      return v ? t("ai_model_text", { auc: FC.fmt(v.auc, 2), clim: FC.fmt(v.auc_climatology, 2), hit: Math.round(v.hit_rate_when_high * 100), base: Math.round(v.event_rate_test * 100) }) : t("ai_err");
    }
    if (intent === "tg") {
      if (FC.openAlerts) FC.openAlerts();
      return t("ai_alerts_text") + (st.bot ? `
${t("ai_alerts_tg", { url: `https://t.me/${st.bot}` })}` : "");
    }
    return t("ai_offline_help");
  }
  function dist(a, b) {
    const R = 6371, r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(x));
  }

  /* ---------- telegram link (optional) ---------- */
  FC.getJSON("/api/links").then((l) => {
    st.bot = l && l.telegram_bot;
    document.body.classList.toggle("has-tg", !!st.bot);
    translate();
  }).catch(() => translate());
  translate();
})();
