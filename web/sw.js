/* FireCalendar service worker: shows fire alerts sent by Web Push, even when the site is closed. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "FireCalendar", {
    body: d.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/badge-96.png",
    tag: d.tag || "fc-alert",
    renotify: true,
    data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) { await c.focus(); return c.navigate(url); }
    }
    return self.clients.openWindow(url);
  })());
});
