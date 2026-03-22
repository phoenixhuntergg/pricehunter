const CACHE = 'pricehunter-v2';
const ASSETS = ['/', '/index.html'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS))); self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener('fetch', e => {
  if (e.request.url.includes('onrender.com') || e.request.url.includes('api.telegram') || e.request.url.includes('localhost')) return;
  e.respondWith(caches.match(e.request).then(cached => { const net = fetch(e.request).then(r => { if (r.ok && e.request.method === 'GET') { caches.open(CACHE).then(c => c.put(e.request, r.clone())); } return r; }).catch(() => cached); return cached || net; }));
});
