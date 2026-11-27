/* Service worker: makes the dashboard installable and usable offline, and shows
   "new stock added" notifications.
   App files: cache first, refreshed in the background.
   Screen data and price history: network first, cached copy when offline.
   Notifications arrive two ways, de-duplicated by the refresh's run time:
     - push: sent by the daily workflow right after a refresh (tv_notify.py)
     - periodicsync: Chrome wakes the installed app now and then to check. */
const VERSION = "v4";
const SHELL = ["./", "index.html", "logos.js", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];
const STATE = "state-store";          // not versioned: survives app updates
const STATE_KEY = "./__notify-state";

self.addEventListener("install", e => {
  e.waitUntil(caches.open(`shell-${VERSION}`).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== STATE && !k.endsWith(VERSION)).map(k => caches.delete(k))))
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
    e.respondWith(fetch(e.request).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(`data-${VERSION}`).then(c => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request, { ignoreSearch: true })));
    return;
  }

  e.respondWith(caches.open(`shell-${VERSION}`).then(async c => {
    const hit = await c.match(e.request, { ignoreSearch: true });
    const net = fetch(e.request).then(res => { if (res.ok || res.type === "opaque") c.put(e.request, res.clone()); return res; }).catch(() => hit);
    return hit || net;
  }));
});

/* ---------- notifications ---------- */
async function getState() {
  const r = await (await caches.open(STATE)).match(STATE_KEY);
  return r ? r.json() : {};
}
async function setState(s) {
  await (await caches.open(STATE)).put(STATE_KEY, new Response(JSON.stringify(s), { headers: { "Content-Type": "application/json" } }));
}

/* Same headline rules as tv_notify.py: name one or two tickers, otherwise give the count. */
function messageFor(doc) {
  const added = (doc.changes && doc.changes.added) || [];
  if (!added.length) return null;
  const by = Object.fromEntries((doc.matches || []).map(m => [m.ticker, m]));
  const title = added.length === 1 ? `${added[0]} added to Wounded Mid-Caps`
    : added.length === 2 ? `${added[0]} and ${added[1]} added to Wounded Mid-Caps`
    : `${added.length} stocks added to Wounded Mid-Caps`;
  const line = t => by[t] ? `${t} ${Math.round(by[t].drawdown_pct)}% below its high` : t;
  const body = added.slice(0, 4).map(line).join(" · ") + (added.length > 4 ? ` · +${added.length - 4} more` : "");
  return { title, body, tickers: added, run_at: doc.run_at, url: "./" };
}

async function notifyOnce(msg) {
  if (!msg) return;
  const st = await getState();
  if (msg.run_at && st.lastNotifiedRun === msg.run_at) return;   // already shown for this refresh
  st.lastNotifiedRun = msg.run_at; st.lastSeenRun = msg.run_at;
  await setState(st);
  await self.registration.showNotification(msg.title, {
    body: msg.body, icon: "icons/icon-192.png", badge: "icons/icon-192.png",
    tag: "screen-added", renotify: true, data: { url: msg.url || "./" },
  });
}

self.addEventListener("push", e => {
  let msg = null;
  try { msg = e.data ? e.data.json() : null; } catch (err) {}
  e.waitUntil(notifyOnce(msg));
});

/* Background check: only notify for a refresh this device hasn't seen yet. */
async function checkForNewRun() {
  const res = await fetch(`data/latest.json?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) return;
  const doc = await res.json();
  const st = await getState();
  if (!st.lastSeenRun) { st.lastSeenRun = doc.run_at; await setState(st); return; }  // first check sets a baseline
  if (doc.run_at === st.lastSeenRun) return;
  st.lastSeenRun = doc.run_at; await setState(st);
  await notifyOnce(messageFor(doc));
}
self.addEventListener("periodicsync", e => { if (e.tag === "screen-check") e.waitUntil(checkForNewRun()); });

/* The page tells us which refresh it is showing, so opening the app also counts as "seen". */
self.addEventListener("message", e => {
  if (e.data && e.data.type === "seen" && e.data.run_at) {
    e.waitUntil(getState().then(st => { st.lastSeenRun = e.data.run_at; if (!st.lastNotifiedRun) st.lastNotifiedRun = null; return setState(st); }));
  }
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || "./", self.registration.scope).href;
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) if (c.url.startsWith(self.registration.scope)) { c.navigate(target).catch(() => {}); return c.focus(); }
    return clients.openWindow(target);
  }));
});
