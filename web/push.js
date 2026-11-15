/* FireCalendar browser fire alerts (Web Push). Works in Chrome, Edge, Firefox, Samsung Internet on Android
   and desktop; on iPhone/iPad (iOS 16.4+) after "Add to Home Screen". No account and no Telegram needed. */
(() => {
  "use strict";
  const FC = window.FC;
  if (!FC) return;
  const $ = (id) => document.getElementById(id);
  const t = (k, v) => FC.t(k, v);
  const KEY = "fc-push";
  const st = { cfg: null, saved: null };
  try { st.saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { st.saved = null; }
  const save = (v) => { st.saved = v; try { v ? localStorage.setItem(KEY, JSON.stringify(v)) : localStorage.removeItem(KEY); } catch (e) { /* storage blocked */ } };

  const supported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const BOX = { w: 46, s: 35, e: 88, n: 56 };
  const inside = (p) => p && p.lat >= BOX.s && p.lat <= BOX.n && p.lon >= BOX.w && p.lon <= BOX.e;

  function msg(text) { $("push-msg").hidden = !text; $("push-msg").textContent = text || ""; }
  function show() {
    const on = !!st.saved;
    $("push-setup").hidden = on;
    $("push-active").hidden = !on;
    $("alerts-btn").classList.toggle("on", on);
    if (on) $("push-status").textContent = t("al_status", { r: st.saved.radius_km, lat: FC.fmt(st.saved.lat, 2), lon: FC.fmt(st.saved.lon, 2) });
  }
  function keyBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  function locate() {
    return new Promise((res) => {
      if (!navigator.geolocation) return res(null);
      navigator.geolocation.getCurrentPosition((p) => res({ lat: p.coords.latitude, lon: p.coords.longitude }), () => res(null),
        { timeout: 10000, maximumAge: 300000 });
    });
  }
  async function registration() {
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    return reg;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail ? (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)) : `HTTP ${r.status}`);
    return j;
  }

  async function turnOn() {
    msg("");
    if (!st.cfg || !st.cfg.enabled) return msg(t("al_off_server"));
    if (!supported()) return msg(t("al_unsupported"));
    const btn = $("push-on"), label = btn.querySelector("span");
    const where = $("push-where").value, r = +$("push-radius").value;
    btn.disabled = true; label.textContent = t("al_busy");
    try {
      const p = where === "me" ? await locate() : FC.areaCenter();
      if (!p) return msg(where === "me" ? t("al_geo") : t("al_noarea"));
      if (!inside(p)) return msg(t("al_outside"));
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return msg(t("al_denied"));
      const reg = await registration();
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(st.cfg.public_key) });
      const j = await post("/api/push/subscribe", { subscription: sub.toJSON(), lat: p.lat, lon: p.lon, radius_km: r, lang: FC.S.lang });
      save({ lat: j.lat, lon: j.lon, radius_km: j.radius_km });
      show();
    } catch (e) {
      msg(t("al_err", { e: e.message || e }));
    } finally {
      btn.disabled = false; label.textContent = t("al_on");
    }
  }
  async function current() {
    const reg = await navigator.serviceWorker.getRegistration("/");
    return reg ? reg.pushManager.getSubscription() : null;
  }
  async function test() {
    msg("");
    try {
      const sub = await current();
      if (!sub || !st.saved) { save(null); show(); return; }
      await post("/api/push/test", { subscription: sub.toJSON(), lat: st.saved.lat, lon: st.saved.lon, radius_km: st.saved.radius_km, lang: FC.S.lang });
      msg(t("al_sent"));
    } catch (e) { msg(t("al_err", { e: e.message || e })); }
  }
  async function turnOff() {
    msg("");
    try {
      const sub = await current();
      if (sub) {
        await post("/api/push/unsubscribe", { endpoint: sub.endpoint, lang: FC.S.lang }).catch(() => {});
        await sub.unsubscribe();
      }
    } catch (e) { /* already gone */ }
    save(null); show();
  }

  /* the alerts window opens from the bell in the top bar (and from the assistant), independent of the panels */
  function openDlg(on) {
    $("alerts-dlg").hidden = !on;
    if (on) { show(); $("push-on").focus(); } else $("alerts-btn").focus();
  }
  FC.openAlerts = () => openDlg(true);
  $("alerts-btn").onclick = () => openDlg(true);
  $("alerts-close").onclick = () => openDlg(false);
  $("alerts-dlg").onclick = (e) => { if (e.target === $("alerts-dlg")) openDlg(false); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("alerts-dlg").hidden) openDlg(false); });
  $("push-on").onclick = turnOn;
  $("push-test").onclick = test;
  $("push-off").onclick = turnOff;
  document.addEventListener("fc:lang", show);

  (async function init() {
    show();
    try { st.cfg = await FC.getJSON("/api/push/config"); } catch (e) { st.cfg = { enabled: false }; }
    if (!st.cfg.enabled) { msg(t("al_off_server")); return; }
    if (!supported()) { msg(isIOS() ? t("al_unsupported") : t("al_unsupported_other")); $("push-on").disabled = true; return; }
    // the browser may have dropped the subscription (cleared data, revoked permission)
    if (st.saved) { const sub = await current().catch(() => null); if (!sub || Notification.permission !== "granted") { save(null); show(); } }
  })();
})();
