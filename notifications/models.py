from django.conf import settings
from django.db import models


class Notification(models.Model):
    """
    One row per in-app notification - the "Notification Center" shown at
    /notifications/ and summarised by the bell icon in the navbar. Created
    by bookmarket.notify.dispatch() alongside whichever other channels
    (email/push/SMS/WhatsApp) a given event is configured to use - see
    that module for the fan-out logic.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=500, blank=True, help_text='Where clicking this notification should go.')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username}: {self.title}'


class PushSubscription(models.Model):
    """
    One row per browser/device a user has enabled Web Push on - the
    subscription object PushManager.subscribe() returns client-side (see
    static/js/push.js), needed to address a push message to that specific
    browser via bookmarket.notify.send_push().
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} ({self.endpoint[:40]}...)'


class NotificationPreference(models.Model):
    """
    Per-user channel opt-in/out, editable at /notifications/preferences/.
    Email/push/in-app default on since they're free; SMS/WhatsApp default
    off since each message costs money via Twilio - and most events don't
    warrant a text anyway, see bookmarket.notify.dispatch()'s `allow_phone`
    flag for which events even offer a phone channel at all.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preference',
    )
    email_enabled = models.BooleanField(default=True)
    push_enabled = models.BooleanField(default=True)
    in_app_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=False)
    whatsapp_enabled = models.BooleanField(default=False)

    @classmethod
    def for_user(cls, user):
        prefs, _ = cls.objects.get_or_create(user=user)
        return prefs

    def __str__(self):
        return f'Notification preferences: {self.user.username}'
