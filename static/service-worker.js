const VERSION = "portexa-pwa-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();

      await Promise.all(
        keys
          .filter((key) => key.startsWith("portexa-pwa-"))
          .map((key) => caches.delete(key))
      );

      await self.clients.claim();
    })()
  );
});

// جميع الطلبات تذهب مباشرة إلى الشبكة.
// لا يتم تخزين صفحات الحسابات أو بيانات الشحنات.
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  if (new URL(event.request.url).origin !== self.location.origin) {
    return;
  }

  event.respondWith(fetch(event.request));
});
