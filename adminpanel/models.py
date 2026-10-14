import json

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _

MAX_USER_AGENT = 255
MAX_SNAPSHOT_CHARS = 4000


class AuditLogQuerySet(models.QuerySet):
    """Rows are written once and never edited or removed through the application."""

    def update(self, **kwargs):
        raise PermissionDenied('Audit log entries are append-only.')

    def bulk_update(self, *args, **kwargs):
        raise PermissionDenied('Audit log entries are append-only.')

    def delete(self):
        raise PermissionDenied('Audit log entries are append-only.')


def _snapshot(value):
    """JSON-safe copy of an old/new value (Decimals, dates, model instances -> text), size-capped."""
    if value is None:
        return None
    clean = json.loads(json.dumps(value, cls=DjangoJSONEncoder, default=str))
    if len(json.dumps(clean)) > MAX_SNAPSHOT_CHARS:
        return {'truncated': True}
    return clean


class AuditLog(models.Model):
    """
    One row per action taken from the Staff Dashboard (adminpanel/views.py)
    - who did what, to what, and when. Django admin's own actions (e.g. the
    Razorpay/wallet refunds in orders/admin.py:ReturnRequestAdmin) aren't
    logged here; this only covers the three actions this app's own views
    expose. Write-only from the app's point of view - nothing reads these
    back yet beyond the raw Django admin changelist, but having them from
    day one means moderation history exists once more than one staff
    account uses this panel.
    """
    ACTION_CHOICES = (
        ('approve_book', _('Approved listing')),
        ('remove_book', _('Removed listing')),
        ('clear_duplicate_flag', _('Cleared duplicate-account flag')),
        ('dismiss_report', _('Dismissed listing report')),
        ('clear_fraud_flag', _('Cleared fraud flag (kept listing live)')),
        ('approve_partner', _('Approved Shopkeeper Partner application')),
        ('reject_partner', _('Rejected Shopkeeper Partner application')),
        ('view_partner_proof', _('Viewed a partner ID/shop proof document')),
        ('accept_donation', _('Accepted a book donation')),
        ('reject_donation', _('Rejected a book donation')),
        ('issue_certificate', _('Issued a donation certificate')),
        ('revoke_certificate', _('Revoked a donation certificate')),
        ('mark_payout_paid', _('Recorded a seller UPI payout as paid')),
        ('hold_payout', _('Put a seller payout on hold')),
        ('release_payout_hold', _('Released a hold on a seller payout')),
        ('cancel_payout', _('Cancelled a seller payout')),
        ('resolve_dispute', _('Resolved a buyer dispute')),
        ('staff_2fa_enrolled', _('Enrolled two-factor authentication')),
        ('staff_2fa_recovery_used', _('Signed in with a 2FA recovery code')),
        ('staff_2fa_codes_regenerated', _('Regenerated 2FA recovery codes')),
        ('staff_2fa_locked', _('Locked out after wrong 2FA codes')),
        ('staff_2fa_reset', _('Two-factor authentication reset from the console')),
        ('staff_role_changed', _('Staff role granted or revoked')),
        ('refund_issued', _('Issued a refund')),
        ('refund_marked_done', _('Marked a pending refund as done')),
        ('refund_claim_released', _('Released a stuck pending refund')),
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='adminpanel_actions',
        help_text='The staff member who took the action. Kept even if the target is later deleted.',
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    target_repr = models.CharField(max_length=255, help_text='Human-readable label for what was acted on, e.g. a book title or username.')
    target_id = models.PositiveIntegerField(null=True, blank=True)
    note = models.TextField(blank=True, help_text='Extra context, e.g. the duplicate-flag reason that was cleared.')
    created_at = models.DateTimeField(auto_now_add=True)
    # Where the action came from, and what it changed. All optional: older rows and console
    # commands have no request, and many actions have no meaningful "before" value.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=MAX_USER_AGENT, blank=True)
    old_value = models.JSONField(null=True, blank=True, help_text='The value(s) before the change.')
    new_value = models.JSONField(null=True, blank=True, help_text='The value(s) after the change.')
    reason = models.CharField(max_length=255, blank=True, help_text='Why the staff member did it, if they said.')

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        # Capability permissions for staff roles - see adminpanel/roles.py. They are attached to
        # this model only because Django needs a model to hang custom permissions on.
        permissions = [
            ('cap_moderation', 'Staff: review, approve and remove listings and reports'),
            ('cap_fraud', 'Staff: clear fraud and duplicate-account flags'),
            ('cap_partners', 'Staff: review partner applications and open ID documents'),
            ('cap_finance', 'Staff: payouts, revenue and refunds'),
            ('cap_disputes', 'Staff: handle disputes and return requests'),
            ('cap_donations', 'Staff: handle donations and certificates'),
            ('cap_audit', 'Staff: read the full audit log'),
        ]

    def __str__(self):
        actor_name = self.actor.username if self.actor else 'deleted user'
        return f'{actor_name} {self.get_action_display()} - {self.target_repr}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied('Audit log entries are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied('Audit log entries are append-only.')

    @classmethod
    def record(cls, *, actor, action, target_repr, target_id=None, note='', request=None, old=None, new=None, reason=''):
        """Append one entry. Pass `request` to capture the client IP and browser, and `old`/`new`
        (dicts of the changed fields) to capture what changed."""
        ip, agent = None, ''
        if request is not None:
            from accounts.throttle import get_client_ip
            candidate = get_client_ip(request)
            try:
                from django.core.validators import validate_ipv46_address
                validate_ipv46_address(candidate)
                ip = candidate
            except Exception:
                ip = None
            agent = (request.META.get('HTTP_USER_AGENT', '') or '')[:MAX_USER_AGENT]
        return cls.objects.create(
            actor=actor, action=action, target_repr=target_repr[:255], target_id=target_id, note=note,
            ip_address=ip, user_agent=agent, old_value=_snapshot(old), new_value=_snapshot(new), reason=(reason or '')[:255],
        )
