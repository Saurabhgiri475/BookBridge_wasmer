from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Coupon(models.Model):
    """
    A promo code an admin creates (via Django admin) that a buyer can enter
    at checkout for a discount. Deliberately kept separate from the
    Referral/Cashback rewards in wallet/services.py, since those are
    per-user, ledger-based wallet credits, while a coupon is a shared,
    admin-defined code with its own rules and a redemption cap.
    """
    DISCOUNT_TYPE_CHOICES = (
        ('percent', _('Percentage off')),
        ('flat', _('Flat amount off')),
    )

    code = models.CharField(max_length=24, unique=True, help_text='Shown/typed in ALL CAPS, e.g. WELCOME50.')
    description = models.CharField(max_length=255, blank=True, help_text='Internal note / shown to the buyer, e.g. "10% off, up to \u20b9100".')

    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPE_CHOICES, default='percent')
    discount_value = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text="Percentage (e.g. 10 for 10%) or a flat rupee amount, depending on Discount type.",
    )
    max_discount_amount = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Caps the discount for percentage coupons (e.g. '10% off, up to \u20b9100'). Ignored for flat coupons.",
    )
    min_order_amount = models.DecimalField(
        max_digits=8, decimal_places=2, default=0,
        help_text="Book price must be at least this much for the coupon to apply (delivery charge isn't counted).",
    )

    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True, help_text='Leave blank for no expiry.')

    usage_limit_total = models.PositiveIntegerField(
        null=True, blank=True, help_text='Max redemptions across all users combined. Leave blank for unlimited.',
    )
    usage_limit_per_user = models.PositiveIntegerField(default=1, help_text='Max redemptions per individual buyer.')
    first_order_only = models.BooleanField(default=False, help_text="Only usable on a buyer's very first order.")

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    def is_within_validity_window(self, at=None):
        at = at or timezone.now()
        if self.valid_from and at < self.valid_from:
            return False
        if self.valid_until and at > self.valid_until:
            return False
        return True

    def total_redemptions(self):
        return self.redemptions.count()

    def compute_discount(self, order_amount):
        """Returns the rupee discount for a given book-price `order_amount` (never more than that amount)."""
        order_amount = Decimal(order_amount)
        if self.discount_type == 'percent':
            discount = (order_amount * self.discount_value / Decimal('100'))
            if self.max_discount_amount is not None:
                discount = min(discount, self.max_discount_amount)
        else:
            discount = self.discount_value
        discount = max(Decimal('0'), min(discount, order_amount))
        return discount.quantize(Decimal('0.01'))


class CouponRedemption(models.Model):
    """
    One row per successful coupon use, tied 1:1 to the Order it was applied
    to. `order` cascades on delete - if an order is deleted before payment
    completes (see orders/views.py's insufficient-balance / gateway-error
    paths), the redemption disappears with it and the coupon becomes usable
    again, rather than "spending" a use on an order that never happened.
    """
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='redemptions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='coupon_redemptions')
    order = models.OneToOneField('orders.Order', on_delete=models.CASCADE, related_name='coupon_redemption')
    amount_discounted = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.coupon.code} - {self.user.username} (\u20b9{self.amount_discounted})"
