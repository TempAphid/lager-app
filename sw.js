self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open('lager-app-v1').then((cache) => {
      return cache.addAll(['/', '/?app=1']);
    })
  );
});

self.addEventListener('fetch', (e) => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});