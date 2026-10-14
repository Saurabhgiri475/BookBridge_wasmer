// push.js
// Wires up the "Enable Push Notifications" toggle on the preferences page
// (templates/notifications/preferences.html): registers the service
// worker, asks the browser for Notification permission, subscribes via
// PushManager using the site's VAPID public key (rendered into the page
// as data-vapid-key by notifications/context_processors.py), then POSTs
// the subscription to notifications:push_subscribe so the server can
// address this browser later (see bookmarket/notify.py:send_push).

function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
}

async function enablePush(vapidPublicKey) {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        alert('Push notifications are not supported in this browser.');
        return false;
    }
    if (!vapidPublicKey) {
        alert('Push notifications are not configured on this server yet (missing VAPID key).');
        return false;
    }

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
        return false;
    }

    const registration = await navigator.serviceWorker.register('/sw.js');
    const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    });

    await fetch('/notifications/push/subscribe/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify(subscription.toJSON()),
    });
    return true;
}

async function disablePush() {
    if (!('serviceWorker' in navigator)) return;
    const registration = await navigator.serviceWorker.getRegistration('/sw.js');
    const subscription = registration && (await registration.pushManager.getSubscription());
    if (subscription) {
        await fetch('/notifications/push/unsubscribe/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
            body: JSON.stringify({ endpoint: subscription.endpoint }),
        });
        await subscription.unsubscribe();
    }
}

document.addEventListener('DOMContentLoaded', function () {
    const toggle = document.getElementById('pushEnableToggle');
    if (!toggle) return;
    const vapidKey = document.body.dataset.vapidKey || '';

    toggle.addEventListener('change', async function () {
        toggle.disabled = true;
        if (toggle.checked) {
            const ok = await enablePush(vapidKey);
            if (!ok) toggle.checked = false;
        } else {
            await disablePush();
        }
        toggle.disabled = false;
    });
});
