// s-tool service worker — minimal app-shell cache so repeat visits to
// any page render from disk in <50ms while the network revalidates in
// the background. Strategy:
//   - HTML/CSS/JS routes: stale-while-revalidate (return cached, refresh)
//   - /api/*: pass through to network (always live data)
//   - Fonts: cache-first (immutable per version)

const VERSION = 'v2';
const SHELL_CACHE = 's-tool-shell-' + VERSION;
const API_PATH = '/api/';
const SHELL_PATHS = [
  '/app/', '/picks/',
  '/shared/skeleton.css',
];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // Best-effort prefetch of the app shell. Don't fail install if any
    // route 404s — service worker still registers and SWR works for
    // whatever IS cached.
    await Promise.allSettled(SHELL_PATHS.map(p => cache.add(p).catch(() => null)));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // Drop old caches from prior versions.
    const names = await caches.keys();
    await Promise.all(names.filter(n => n !== SHELL_CACHE).map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith(API_PATH)) return;       // network only for API
  if (url.pathname === '/sw.js') return;               // never cache the SW itself

  e.respondWith((async () => {
    const cache = await caches.open(SHELL_CACHE);
    const cached = await cache.match(req);
    const networkPromise = fetch(req).then((res) => {
      // Only cache successful responses; partial/error responses leak stale 404s.
      if (res && res.ok) cache.put(req, res.clone()).catch(() => {});
      return res;
    }).catch(() => cached);  // offline fallback to cached if any
    return cached || networkPromise;
  })());
});
