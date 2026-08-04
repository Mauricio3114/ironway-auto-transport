const CACHE_NAME = "ironway-pwa-v1";

const STATIC_ASSETS = [
  "/static/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable.png"
];


// ==========================================================
// INSTALAÇÃO
// ==========================================================

self.addEventListener("install", (event) => {

  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        return cache.addAll(STATIC_ASSETS);
      })
  );

  self.skipWaiting();

});


// ==========================================================
// ATIVAÇÃO
// Remove caches antigos
// ==========================================================

self.addEventListener("activate", (event) => {

  event.waitUntil(
    caches.keys()
      .then((cacheNames) => {

        return Promise.all(
          cacheNames.map((cacheName) => {

            if (cacheName !== CACHE_NAME) {
              return caches.delete(cacheName);
            }

          })
        );

      })
  );

  self.clients.claim();

});


// ==========================================================
// FETCH
// Não fazemos cache das páginas administrativas/dinâmicas.
// Cache fica focado nos arquivos estáticos.
// ==========================================================

self.addEventListener("fetch", (event) => {

  if (event.request.method !== "GET") {
    return;
  }

  const url = new URL(event.request.url);

  if (!url.pathname.startsWith("/static/")) {
    return;
  }

  event.respondWith(

    caches.match(event.request)
      .then((cachedResponse) => {

        if (cachedResponse) {
          return cachedResponse;
        }

        return fetch(event.request)
          .then((networkResponse) => {

            if (
              !networkResponse ||
              networkResponse.status !== 200
            ) {
              return networkResponse;
            }

            const responseClone =
              networkResponse.clone();

            caches.open(CACHE_NAME)
              .then((cache) => {
                cache.put(
                  event.request,
                  responseClone
                );
              });

            return networkResponse;

          });

      })

  );

});