/* VocalForge service worker: caches the app shell so it opens offline.
   Shell files are served stale-while-revalidate, so an update lands on the next visit.
   Cross-origin calls (the ElevenLabs API) are never touched. */
const CACHE = 'vocalforge-v1';
const SHELL = ['./', './index.html', './app.js', './app.css', './manifest.json', './icon.svg', './icon-192.png', './icon-512.png', './privacy.html'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  e.respondWith(
    caches.open(CACHE).then(async cache => {
      const cached = await cache.match(e.request);
      const network = fetch(e.request).then(res => {
        if (res.ok) cache.put(e.request, res.clone());
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
