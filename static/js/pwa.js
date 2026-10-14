// pwa.js - registers the service worker, offers "Install app", and prompts for updates.
// No inline handlers (CSP): everything is wired up here.
(function () {
    'use strict';

    // ---- Service worker registration + update prompt ----
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(function (reg) {
                function promptUpdate(worker) {
                    if (document.getElementById('bbUpdateBar')) return;
                    var bar = document.createElement('div');
                    bar.id = 'bbUpdateBar';
                    bar.setAttribute('role', 'status');
                    bar.className = 'position-fixed bottom-0 start-50 translate-middle-x mb-3 px-3 py-2 rounded-3 shadow bg-dark text-light d-flex align-items-center gap-3';
                    bar.style.zIndex = '2000';
                    var msg = document.createElement('span');
                    msg.textContent = 'A new version of BookBridge is available.';
                    var btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'btn btn-warning btn-sm';
                    btn.textContent = 'Refresh';
                    btn.addEventListener('click', function () { worker.postMessage({ type: 'SKIP_WAITING' }); });
                    bar.appendChild(msg);
                    bar.appendChild(btn);
                    document.body.appendChild(bar);
                }
                if (reg.waiting && navigator.serviceWorker.controller) promptUpdate(reg.waiting);
                reg.addEventListener('updatefound', function () {
                    var nw = reg.installing;
                    if (!nw) return;
                    nw.addEventListener('statechange', function () {
                        if (nw.state === 'installed' && navigator.serviceWorker.controller) promptUpdate(nw);
                    });
                });
            }).catch(function () { /* unsupported / blocked: site still works normally */ });

            var reloading = false;
            navigator.serviceWorker.addEventListener('controllerchange', function () {
                if (reloading) return;
                reloading = true;
                window.location.reload();
            });
        });
    }

    // ---- Install button (Chrome/Edge/Android fire beforeinstallprompt) ----
    var deferred = null;
    function installItem() { return document.getElementById('pwaInstallItem'); }

    window.addEventListener('beforeinstallprompt', function (e) {
        e.preventDefault();
        deferred = e;
        var item = installItem();
        if (item) item.hidden = false;
    });

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('pwaInstallBtn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            if (!deferred) return;
            deferred.prompt();
            deferred.userChoice.finally(function () {
                deferred = null;
                var item = installItem();
                if (item) item.hidden = true;
            });
        });
    });

    window.addEventListener('appinstalled', function () {
        deferred = null;
        var item = installItem();
        if (item) item.hidden = true;
    });
})();
