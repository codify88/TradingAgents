// Desk's service worker: makes the app installable and quick to open.
// Only built assets (hashed file names, so never stale) and icons are cached.
// Pages and the API always go to the network: an order plan, a halt, the book
// must never be served from a cache. Offline, the page says so rather than
// showing yesterday's numbers.
const CACHE = "desk-assets-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/assets/") || /^\/icon[-\w]*\.(png|svg)$/.test(url.pathname)) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const hit = await cache.match(event.request);
        if (hit) return hit;
        const res = await fetch(event.request);
        if (res.ok) cache.put(event.request, res.clone());
        return res;
      }),
    );
    return;
  }
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(
        () => new Response(
          "<!doctype html><meta name=viewport content='width=device-width'><body style='font:16px system-ui;padding:24px;background:#f4f2ee;color:#1e1a16'><h1 style='font-size:20px'>Desk is out of reach</h1><p>The Mac or the network is unavailable. Nothing is shown from memory, so nothing here can be out of date. Try again when you are back online.</p>",
          { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 503 },
        ),
      ),
    );
  }
});
