// Shows the bot's pings as notifications from this app, and opens the app on that account when one is tapped.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data.json(); } catch { d = {body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(d.title || "X Trade Bot", {
    body: d.body || "",
    tag: d.tag || undefined,
    data: {url: d.url || "./"},
    icon: "icons/icon-192.png",
    badge: "icons/badge-72.png",
    timestamp: d.ts ? Date.parse(d.ts) : Date.now(),
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "./", self.registration.scope).href;
  e.waitUntil((async () => {
    const open = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const win = open.find(w => w.url.startsWith(self.registration.scope));
    if (win) {
      await win.focus();
      win.postMessage({type: "open", url});  // the page navigates itself (navigate() isn't everywhere)
      return;
    }
    await self.clients.openWindow(url);
  })());
});
