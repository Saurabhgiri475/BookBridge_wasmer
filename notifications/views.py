import json

from django.conf import settings
from django.utils.translation import gettext as _
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from bookmarket.security import safe_next_url, rate_limit
from django.views.decorators.http import require_POST

from .models import Notification, NotificationPreference, PushSubscription


@login_required
def notification_list(request):
    """The in-app Notification Center - every channel's event also lands
    here regardless of the user's email/SMS/WhatsApp/push preferences, so
    there's always one place to see everything (see bookmarket.notify.dispatch)."""
    notifications = Notification.objects.filter(user=request.user)
    paginator = Paginator(notifications, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'notifications/list.html', {
        'page_obj': page_obj,
        'unread_count': notifications.filter(is_read=False).count(),
    })


@require_POST
@login_required
def mark_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    if notification.url:
        return redirect(notification.url)
    return redirect('notifications:list')


@require_POST
@login_required
def mark_all_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return redirect(safe_next_url(request, 'notifications:list'))


@login_required
def preferences(request):
    """Per-channel opt-in/out - see NotificationPreference for the default
    for each channel (email/push/in-app default on, SMS/WhatsApp default
    off since they cost money via Twilio)."""
    prefs = NotificationPreference.for_user(request.user)
    if request.method == 'POST':
        prefs.email_enabled = bool(request.POST.get('email_enabled'))
        prefs.push_enabled = bool(request.POST.get('push_enabled'))
        prefs.in_app_enabled = bool(request.POST.get('in_app_enabled'))
        prefs.sms_enabled = bool(request.POST.get('sms_enabled'))
        prefs.whatsapp_enabled = bool(request.POST.get('whatsapp_enabled'))
        if (prefs.sms_enabled or prefs.whatsapp_enabled) and not request.user.phone:
            messages.warning(
                request,
                _('SMS/WhatsApp are on, but there\'s no phone number on your profile yet - add one so we '
                'actually have somewhere to send them.'),
            )
        prefs.save()
        messages.success(request, _('Notification preferences updated.'))
        return redirect('notifications:preferences')
    return render(request, 'notifications/preferences.html', {
        'prefs': prefs,
        'push_subscriptions': request.user.push_subscriptions.count(),
    })


@require_POST
@login_required
@rate_limit('push-subscribe', 20, 3600, json=True)
def push_subscribe(request):
    """Called by static/js/push.js right after the browser's
    PushManager.subscribe() resolves - stores the subscription so
    bookmarket.notify.send_push() can address this browser later."""
    try:
        data = json.loads(request.body)
        endpoint = data['endpoint']
        p256dh = data['keys']['p256dh']
        auth = data['keys']['auth']
    except (ValueError, KeyError):
        return JsonResponse({'error': 'Malformed push subscription payload.'}, status=400)

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={'user': request.user, 'p256dh': p256dh, 'auth': auth},
    )
    prefs = NotificationPreference.for_user(request.user)
    if not prefs.push_enabled:
        prefs.push_enabled = True
        prefs.save(update_fields=['push_enabled'])
    return JsonResponse({'status': 'subscribed'})


@require_POST
@login_required
def push_unsubscribe(request):
    """Called by push.js when the user disables push in their browser -
    removes just that browser's subscription (a user may have several)."""
    try:
        data = json.loads(request.body)
        endpoint = data.get('endpoint')
    except ValueError:
        endpoint = None

    qs = PushSubscription.objects.filter(user=request.user)
    if endpoint:
        qs = qs.filter(endpoint=endpoint)
    qs.delete()

    if not request.user.push_subscriptions.exists():
        prefs = NotificationPreference.for_user(request.user)
        prefs.push_enabled = False
        prefs.save(update_fields=['push_enabled'])
    return JsonResponse({'status': 'unsubscribed'})
