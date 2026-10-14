from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class PayoutProfile(models.Model):
    """Where a seller wants to be paid. Kept apart from accounts.User so the
    payout destination - the one field an account-takeover attacker would most
    want to change - has its own `updated_at` that staff can see."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payout_profile')
    upi_id = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    # UPI name check. Before the first payout to a UPI ID, staff open their UPI app,
    # type the ID in, and note the account-holder name the app shows - then record it
    # here (payouts.services.record_name_check). It only counts while `verified_upi`
    # still equals `upi_id`, so changing the UPI ID automatically cancels it.
    verified_upi = models.CharField(max_length=100, blank=True)
    verified_name = models.CharField(max_length=120, blank=True, help_text='Account-holder name the UPI app showed.')
    name_check_override = models.BooleanField(
        default=False, help_text='Staff accepted this name even though it did not match the seller\'s account name.',
    )
    name_checked_at = models.DateTimeField(null=True, blank=True)
    name_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='upi_name_checks',
    )

    # Security hold. Changing the payout UPI is the first thing an account takeover does, so
    # after a change no payout is sent for UPI_HOLD_HOURS (payouts.services.UPI_HOLD). Staff can
    # waive it for one specific UPI ID with a written reason; the waiver is cancelled by the next
    # change, exactly like the name check above.
    upi_changed_at = models.DateTimeField(null=True, blank=True, help_text='When the UPI ID last changed (starts the payout hold).')
    hold_override_upi = models.CharField(max_length=100, blank=True)
    hold_override_reason = models.CharField(max_length=300, blank=True)
    hold_override_at = models.DateTimeField(null=True, blank=True)
    hold_override_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='upi_hold_overrides',
    )

    def __str__(self):
        return f'{self.user.username} - {self.upi_id or "no UPI ID"}'

    @property
    def is_name_verified(self):
        return bool(self.upi_id) and self.verified_upi == self.upi_id


class Payout(models.Model):
    """
    What BookBridge owes a seller for one order, and whether it has been sent.

    Created once, in the same transaction that releases the order's payment
    (see payouts.services.ensure_payout), so "delivered" and "has a payout row"
    can't disagree. The money amounts are *snapshots* taken at that moment -
    a later change to the commission rate must not rewrite history that a
    seller has already been shown on a statement.

    Moving the money is still done by a person, outside this app (BookBridge
    does not hold a RazorpayX/bank payout API). Staff send a UPI transfer from
    their own UPI app and record the UTR here; that's the "tracking".
    """
    STATUS_CHOICES = (
        ('pending', _('Pending')),
        ('on_hold', _('On hold')),
        ('paid', _('Paid')),
        ('cancelled', _('Cancelled')),
    )

    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payouts')
    # SET_NULL + the two snapshot fields below: if an order (or its book) is
    # ever deleted, the financial record and statements must survive it.
    order = models.OneToOneField('orders.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='payout')
    order_ref = models.CharField(max_length=8, help_text='First 8 characters of the order id, as shown to users.')
    book_title = models.CharField(max_length=255)

    gross_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=4, decimal_places=1)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True)
    released_at = models.DateTimeField(default=timezone.now, help_text='When the order\'s payment was released (statement date).')

    paid_at = models.DateTimeField(null=True, blank=True)
    paid_to_upi = models.CharField(max_length=100, blank=True, help_text='The UPI ID as it was when the payout was recorded.')
    utr = models.CharField(max_length=30, blank=True, help_text='UPI transaction reference (UTR / RRN) from the transfer.')
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='payouts_recorded',
    )
    note = models.TextField(blank=True)
    refunded_after_payout = models.BooleanField(
        default=False,
        help_text='The buyer was refunded after this payout was already sent - the amount may need recovering from the seller.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-released_at', '-id']
        indexes = [models.Index(fields=['seller', 'status']), models.Index(fields=['seller', 'released_at'])]

    def __str__(self):
        return f'₹{self.net_amount} to {self.seller.username} for order #{self.order_ref} ({self.get_status_display()})'

    @property
    def is_payable(self):
        return self.status == 'pending'

    @staticmethod
    def split(gross, commission_percent):
        """(commission, net) for a sale - same rounding as Order.commission_amount
        so a statement never disagrees with the figure on the order page."""
        gross = Decimal(gross)
        commission = round(gross * Decimal(str(commission_percent)) / 100, 2)
        return commission, gross - commission
