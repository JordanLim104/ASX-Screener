/* Service worker: makes the dashboard installable and usable offline.
   App files: cache first, refreshed in the background.
   Screen data and price history: network first, cached copy when offline. */
const VERSION = "v3";
const SHELL = ["./", "index.html", "logos.js", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(`shell-${VERSION}`).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => !k.endsWith(VERSION)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  const sameOrigin = url.origin === location.origin;
  const isData = sameOrigin && url.pathname.includes("/data/");
  const isFont = url.hostname.endsWith("fonts.googleapis.com") || url.hostname.endsWith("fonts.gstatic.com");
  if (!sameOrigin && !isFont) return;

  if (isData) {
    // Always try for the latest screen; fall back to the last one seen.
    e.respondWith(fetch(e.request).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(`data-${VERSION}`).then(c => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request, { ignoreSearch: true })));
    return;
  }

  // App files and fonts: serve the cached copy at once, update it in the background.
  e.respondWith(caches.open(`shell-${VERSION}`).then(async c => {
    const hit = await c.match(e.request, { ignoreSearch: true });
    const net = fetch(e.request).then(res => { if (res.ok || res.type === "opaque") c.put(e.request, res.clone()); return res; }).catch(() => hit);
    return hit || net;
  }));
});
