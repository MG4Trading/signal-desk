const C = "sd-v2";
self.addEventListener("install", e => { e.waitUntil(caches.open(C).then(c => c.addAll(["./", "index.html"])).catch(() => {})); self.skipWaiting(); });
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(C).then(c => c.put(u.pathname.endsWith("data.json") ? "data.json" : e.request, copy)); return r; })
    .catch(() => caches.match(u.pathname.endsWith("data.json") ? "data.json" : e.request)));
});
