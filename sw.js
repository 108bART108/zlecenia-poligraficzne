const CACHE_NAME = 'zlecenia-shell-v1';
const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Cache'ujemy tylko własną powłokę apki (HTML/CSS/JS/ikony).
// Wszystko inne (Firebase, Firestore, mailto itp.) idzie normalnie przez sieć -
// nie chcemy podszywać się pod dane z bazy.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const isOwnOrigin = url.origin === self.location.origin;
  const isShellFile = SHELL_FILES.some((f) => url.pathname.endsWith(f.replace('./', '')));

  if (isOwnOrigin && isShellFile) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
