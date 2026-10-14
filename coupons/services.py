"""
Coupon validation + redemption.

A discount is computed against the buyer's order subtotal (the book's
final_price) - never the delivery charge - and it comes off what the buyer
pays, not what the seller receives: Order.seller_payout is still derived
from final_price alone (see orders/models.py), so the platform absorbs the
cost of every coupon, the same way it funds the referral/cashback rewards
in wallet/services.py.

validate_coupon() is read-only and safe to call repeatedly (e.g. from an
"Apply" button before the order even exists). redeem_coupon() is the only
function that actually records a use - call it exactly once, right after
the Order row is created, from orders/views.checkout().
"""
from decimal import Decimal

from django.db import transaction

from .models import Coupon, CouponRedemption
from django.utils.translation import gettext as _


class CouponError(Exception):
    """Raised by validate_coupon() with a message that's safe to show directly to the buyer."""


def validate_coupon(code, user, order_amount):
    """
    Returns (coupon, discount_amount) if `code` is currently usable by
    `user` for a book priced at `order_amount`. Raises CouponError with a
    user-facing reason otherwise.
    """
    code = (code or '').strip().upper()
    if not code:
        raise CouponError(_('Enter a coupon code.'))

    coupon = Coupon.objects.filter(code=code).first()
    if not coupon or not coupon.active:
        raise CouponError(_('Invalid coupon code.'))

    if not coupon.is_within_validity_window():
        raise CouponError(_('This coupon has expired or is not active yet.'))

    order_amount = Decimal(order_amount)
    if order_amount < coupon.min_order_amount:
        raise CouponError(_('This coupon needs a minimum book price of ₹%(min_order_amount)s.') % {'min_order_amount': coupon.min_order_amount})

    if coupon.usage_limit_total is not None and coupon.total_redemptions() >= coupon.usage_limit_total:
        raise CouponError(_('This coupon has reached its usage limit.'))

    if not user.is_authenticated:
        raise CouponError(_('Log in to use a coupon.'))

    user_redemptions = CouponRedemption.objects.filter(coupon=coupon, user=user).count()
    if user_redemptions >= coupon.usage_limit_per_user:
        raise CouponError(_("You've already used this coupon the maximum number of times allowed."))

    if coupon.first_order_only:
        from orders.models import Order  # local import: avoids orders <-> coupons import cycle at module load time
        if Order.objects.filter(buyer=user).exclude(status='cancelled').exists():
            raise CouponError(_('This coupon is only valid on your first order.'))

    discount = coupon.compute_discount(order_amount)
    if discount <= 0:
        raise CouponError(_('This coupon does not apply any discount to this order.'))

    return coupon, discount


@transaction.atomic
def redeem_coupon(coupon, user, order, discount_amount):
    """
    Records that `coupon` was used on `order`.

    Safe to call twice for the same order (returns the existing redemption). The usage limits are
    re-checked here, with the coupon row locked: validate_coupon() ran earlier, and without this
    two buyers checking out at the same moment could both pass it and push the coupon past its
    total limit (or one buyer past their per-user limit). Raises CouponError if the limit was
    taken in the meantime - the caller must then cancel the order.
    """
    existing = CouponRedemption.objects.filter(order=order).first()
    if existing:
        return existing
    locked = Coupon.objects.select_for_update().get(pk=coupon.pk)
    if locked.usage_limit_total is not None and locked.redemptions.count() >= locked.usage_limit_total:
        raise CouponError(_('This coupon has reached its usage limit.'))
    if CouponRedemption.objects.filter(coupon=locked, user=user).count() >= locked.usage_limit_per_user:
        raise CouponError(_("You've already used this coupon the maximum number of times allowed."))
    return CouponRedemption.objects.create(
        coupon=locked, user=user, order=order, amount_discounted=discount_amount,
    )
