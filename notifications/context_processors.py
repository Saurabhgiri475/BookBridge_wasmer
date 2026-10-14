from django.conf import settings

from .models import Notification


def notifications(request):
    """Powers the bell icon in base.html - added to TEMPLATES/context_processors
    in settings.py so it's available on every page without each view passing
    it explicitly."""
    if not request.user.is_authenticated:
        return {}
    qs = Notification.objects.filter(user=request.user)
    return {
        'nav_unread_notifications': qs.filter(is_read=False).count(),
        'nav_recent_notifications': qs[:5],
        'webpush_vapid_public_key': settings.WEBPUSH_VAPID_PUBLIC_KEY,
    }
