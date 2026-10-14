from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Wallet(models.Model):
    """One per user. `balance` is only ever changed via wallet.services
    (credit/debit), never edited directly, so it always stays in sync with
    the transaction ledger below."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s wallet - ₹{self.balance}"


class WalletTransaction(models.Model):
    TXN_TYPE_CHOICES = (
        ('credit', _('Credit')),
        ('debit', _('Debit')),
    )
    SOURCE_CHOICES = (
        ('refund', _('Refund')),
        ('cashback', _('Cashback')),
        ('referral', _('Referral Bonus')),
        ('payment', _('Order Payment')),
        ('featured_listing', _('Featured Listing Bump')),
        ('admin_adjustment', _('Admin Adjustment')),
        ('buyback_commission', _('Shopkeeper Partner Buyback Commission')),
    )
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    txn_type = models.CharField(max_length=10, choices=TXN_TYPE_CHOICES)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='wallet_transactions',
    )
    # Makes a money movement exactly-once: a second credit()/debit() with the same key is a no-op
    # that returns this row (see wallet/services.py). Unique, so even a bug can't write it twice.
    idempotency_key = models.CharField(max_length=100, null=True, blank=True, unique=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        sign = '+' if self.txn_type == 'credit' else '-'
        return f"{self.wallet.user.username} {sign}₹{self.amount} ({self.get_source_display()})"
