// notifications-sw.js  (served at /sw.js - see notifications/views.py:service_worker)
//
// BookBridge service worker: Web Push + installable-PWA offline support.
// The placeholders below are filled in per request by the view:
//   __SW_VERSION__ -> cache version (changes whenever the precache list or PWA_VERSION changes)
//   __PRECACHE__   -> JSON array of URLs to precache (offline page + app-shell static files)
//
// Caching policy (deliberately conservative - this is a marketplace with payments,
// wallets, chat and staff tools):
//   * HTML pages are NEVER cached. They are per-user (names, balances, CSRF tokens),
//     so a cached copy could leak to the next person on a shared device or go stale.
//     Navigations are network-first; if the network is down the user sees /offline/.
//   * Only GET requests to our own origin are ever touched; POSTs, Razorpay, CDNs,
//     /admin/, /staff/, /orders/, /wallet/, /payouts/, /chat/, /accounts/ etc. pass through.
//   * /static/  -> stale-while-revalidate (files are content-hashed in production)
//   * /media/   -> cache-first with a size cap (public book cover photos only;
//                  private documents are served by authorised views, not /media/)

const VERSION = '__SW_VERSION__';
const PRECACHE = __PRECACHE__;
const OFFLINE_URL = '/offline/';
const STATIC_CACHE = 'bb-static-' + VERSION;
const MEDIA_CACHE = 'bb-media-v1';
const MEDIA_MAX_ENTRIES = 80;

// ---------------------------------------------------------------- lifecycle

self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(STATIC_CACHE).then(function (cache) {
            // Individually, so one missing file can't break the whole install.
            return Promise.all(PRECACHE.map(function (url) {
                return cache.add(new Request(url, { cache: 'reload' })).catch(function () {});
            }));
        })
    );
    // No skipWaiting() here: activating mid-session could swap assets under an open
    // checkout/form. pwa.js shows an "Update available" prompt and sends SKIP_WAITING.
});

self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys.map(function (key) {
                const isOurs = key.indexOf('bb-static-') === 0;
                if (isOurs && key !== STATIC_CACHE) return caches.delete(key);
            }));
        }).then(function () {
            return self.clients.claim();
        })
    );
});

self.addEventListener('message', function (event) {
    if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
    if (event.data && event.data.type === 'CLEAR_CACHES') {
        event.waitUntil(caches.keys().then(function (keys) {
            return Promise.all(keys.map(function (k) { return caches.delete(k); }));
        }));
    }
});

// ---------------------------------------------------------------- fetch

function trimCache(cacheName, maxEntries) {
    return caches.open(cacheName).then(function (cache) {
        return cache.keys().then(function (keys) {
            if (keys.length <= maxEntries) return;
            return cache.delete(keys[0]).then(function () { return trimCache(cacheName, maxEntries); });
        });
    });
}

function staleWhileRevalidate(request) {
    return caches.open(STATIC_CACHE).then(function (cache) {
        return cache.match(request).then(function (cached) {
            const network = fetch(request).then(function (response) {
                if (response && response.ok) cache.put(request, response.clone());
                return response;
            }).catch(function () { return cached; });
            return cached || network;
        });
    });
}

function cacheFirstMedia(request) {
    return caches.open(MEDIA_CACHE).then(function (cache) {
        return cache.match(request).then(function (cached) {
            if (cached) return cached;
            return fetch(request).then(function (response) {
                if (response && response.ok) {
                    cache.put(request, response.clone()).then(function () {
                        trimCache(MEDIA_CACHE, MEDIA_MAX_ENTRIES);
                    });
                }
                return response;
            });
        });
    });
}

self.addEventListener('fetch', function (event) {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;           // CDNs, Razorpay, maps: untouched
    if (url.pathname === '/sw.js') return;

    // Page navigations: network-first, offline page as the fallback. Never cached.
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request).catch(function () {
                return caches.match(OFFLINE_URL, { ignoreSearch: true }).then(function (r) {
                    return r || new Response('You are offline.', {
                        status: 503, headers: { 'Content-Type': 'text/plain' },
                    });
                });
            })
        );
        return;
    }

    if (url.pathname.indexOf('/static/') === 0) {
        event.respondWith(staleWhileRevalidate(request));
        return;
    }
    if (url.pathname.indexOf('/media/') === 0) {
        event.respondWith(cacheFirstMedia(request));
        return;
    }
    // Everything else (JSON/AJAX endpoints, etc.): straight to the network.
});

// ---------------------------------------------------------------- web push

self.addEventListener('push', function (event) {
    let payload = { title: 'BookBridge', body: 'You have a new notification.', url: '/' };
    if (event.data) {
        try {
            payload = event.data.json();
        } catch (err) {
            payload.body = event.data.text();
        }
    }
    event.waitUntil(
        self.registration.showNotification(payload.title || 'BookBridge', {
            body: payload.body || '',
            icon: '/static/img/icons/icon-192.png',
            badge: '/static/img/icons/badge-96.png',
            data: { url: payload.url || '/' },
        })
    );
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    const target = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin);
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (windowClients) {
            for (const client of windowClients) {
                if (client.url === target.href && 'focus' in client) return client.focus();
            }
            if (clients.openWindow) return clients.openWindow(target.href);
        })
    );
});
