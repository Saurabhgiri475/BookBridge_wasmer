"""
Payout business logic. Views and the order-release code call these; nothing
else changes a Payout's status directly, so the rules live in one place.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import Payout, PayoutProfile
from .namecheck import names_match
from .upi import is_valid_upi_id, normalize_upi_id
from django.utils.translation import gettext as _, ngettext

# UPI UTRs are 12 digits; other banks' references can be alphanumeric. Accept
# 6-30 letters/digits so a typo like a pasted space or "UTR:" prefix is caught
# without rejecting legitimate references.
UTR_RE = re.compile(r'^[A-Za-z0-9]{6,30}$')

ZERO = Decimal('0.00')

# No payout is sent for this long after the seller's UPI ID changes (account-takeover defence).
UPI_HOLD = timedelta(hours=getattr(settings, 'PAYOUT_UPI_HOLD_HOURS', 72))


class PayoutError(Exception):
    """A staff action that can't be done - the message is shown to the user."""


class NameMismatch(PayoutError):
    """The name the UPI app showed does not match the seller's account name."""


def get_upi_id(user):
    profile = PayoutProfile.objects.filter(user=user).first()
    return profile.upi_id if profile else ''


def set_upi_id(user, upi_id, *, start_hold=False):
    """Save the seller's payout UPI ID. With start_hold=True, an actual change (or the first
    time an ID is set) starts the payout hold. The seller-facing view always passes True;
    it is a parameter only so back-office code and fixtures can set an ID without one."""
    upi_id = normalize_upi_id(upi_id)
    if not is_valid_upi_id(upi_id):
        raise PayoutError(_('Enter a valid UPI ID, like name@bank.'))
    defaults = {'upi_id': upi_id}
    if start_hold:
        previous = PayoutProfile.objects.filter(user=user).values_list('upi_id', flat=True).first() or ''
        if previous != upi_id:
            defaults['upi_changed_at'] = timezone.now()
    profile, _created = PayoutProfile.objects.update_or_create(user=user, defaults=defaults)
    return profile


def upi_hold_remaining(profile, now=None):
    """Time left on the post-change payout hold (timedelta), or None when nothing blocks payment.
    A staff waiver only counts while it was granted for the UPI ID that is on file now."""
    if not profile or not profile.upi_changed_at:
        return None
    if profile.hold_override_at and profile.hold_override_upi == profile.upi_id:
        return None
    left = profile.upi_changed_at + UPI_HOLD - (now or timezone.now())
    return left if left > timedelta(0) else None


def format_hold(left):
    hours = int(left.total_seconds() // 3600) + 1  # round up so "0 hours" is never shown while it still blocks
    return ngettext('%(hours)d hour', '%(hours)d hours', hours) % {'hours': hours}


@transaction.atomic
def override_upi_hold(seller, *, actor, reason):
    """Staff waive the hold for the UPI ID currently on file. A real reason is mandatory; the caller audits it."""
    reason = (reason or '').strip()
    if len(reason) < 10:
        raise PayoutError(_('Explain why it is safe to pay before the hold ends (at least 10 characters) - e.g. how you confirmed it is really the seller.'))
    profile = PayoutProfile.objects.select_for_update().filter(user=seller).first()
    if not profile or not profile.upi_id:
        raise PayoutError(_('%(username)s has no UPI ID on file.') % {'username': seller.username})
    if not upi_hold_remaining(profile):
        raise PayoutError(_('There is no active hold to override for this UPI ID.'))
    profile.hold_override_upi = profile.upi_id
    profile.hold_override_reason = reason[:300]
    profile.hold_override_at = timezone.now()
    profile.hold_override_by = actor
    profile.save(update_fields=['hold_override_upi', 'hold_override_reason', 'hold_override_at', 'hold_override_by', 'updated_at'])
    return profile


def needs_name_check(seller, upi_id=None):
    """
    True when staff must confirm the UPI account-holder name before paying this
    seller. Only the *first* payout to a given UPI ID needs it: once a transfer to
    that exact ID has been recorded as paid, later ones don't. Changing the UPI ID
    starts the check again (the recorded check is tied to the ID it was done for).
    """
    profile = PayoutProfile.objects.filter(user=seller).first()
    upi_id = upi_id if upi_id is not None else (profile.upi_id if profile else '')
    if not upi_id:
        return False
    if profile and profile.is_name_verified:
        return False
    return not Payout.objects.filter(seller=seller, status='paid', paid_to_upi=upi_id).exists()


@transaction.atomic
def record_name_check(seller, upi_name, *, actor, override=False, note=''):
    """
    Staff record the account-holder name their UPI app showed for the seller's
    UPI ID. If it matches the seller's account name it is accepted. If not, it is
    refused (NameMismatch) unless `override` is set with a note saying why - e.g. the
    UPI account is a family member's, or the seller's profile name is a nickname.
    Returns (profile, matched).
    """
    profile = PayoutProfile.objects.select_for_update().filter(user=seller).first()
    if not profile or not profile.upi_id:
        raise PayoutError(_('%(username)s has not added a UPI ID yet.') % {'username': seller.username})
    upi_name = re.sub(r'\s+', ' ', (upi_name or '')).strip()
    if len(upi_name) < 2 or len(upi_name) > 120 or not re.search(r'[A-Za-z]{2}', upi_name):
        raise PayoutError(_('Type the account-holder name exactly as your UPI app shows it.'))
    account_name = seller.get_full_name().strip()
    matched = names_match(account_name, upi_name)
    note = (note or '').strip()
    if not matched:
        if not override:
            shown = account_name or 'no name on their profile'
            raise NameMismatch(
                _('The UPI app shows "%(upi_name)s" but the seller\'s account name is "%(shown)s". If this is still the right person, tick "Accept anyway" and say why.') % {'upi_name': upi_name, 'shown': shown}
            )
        if not note:
            raise PayoutError(_('Add a note explaining why you are accepting a name that does not match.'))
    profile.verified_upi = profile.upi_id
    profile.verified_name = upi_name
    profile.name_check_override = not matched
    profile.name_checked_at = timezone.now()
    profile.name_checked_by = actor
    profile.save(update_fields=[
        'verified_upi', 'verified_name', 'name_check_override', 'name_checked_at', 'name_checked_by', 'updated_at',
    ])
    return profile, matched


@transaction.atomic
def ensure_payout(order):
    """
    Create the Payout for `order` if it doesn't exist yet. Safe to call more
    than once (the Razorpay callback and webhook can both reach the release
    path), and meant to be called from inside the transaction that marks the
    payment released.
    """
    # Re-read: a still-in-memory Order can carry the float default for
    # platform_commission_percent (see orders.views._deliver_digital_order).
    from orders.models import Order
    order = Order.objects.select_related('book__seller').get(pk=order.pk)
    commission, net = Payout.split(order.final_price, order.platform_commission_percent)
    payout, created = Payout.objects.get_or_create(
        order=order,
        defaults={
            'seller': order.book.seller,
            'order_ref': str(order.order_id)[:8],
            'book_title': order.book.title[:255],
            'gross_amount': order.final_price,
            'commission_percent': order.platform_commission_percent,
            'commission_amount': commission,
            'net_amount': net,
        },
    )
    if created:
        # A buyer can dispute an order before delivery is confirmed; the payout
        # that delivery creates must then start on hold, not pending.
        from disputes.services import hold_payout_for_active_dispute
        hold_payout_for_active_dispute(order, payout)
    return payout


@transaction.atomic
def mark_paid(payout_ids, *, utr, actor):
    """
    Record that staff sent one UPI transfer covering `payout_ids`. All of them
    must belong to the same seller (one transfer = one payee), still be
    pending, and the seller must have a UPI ID on file. Returns the payouts.
    """
    utr = (utr or '').strip()
    if not UTR_RE.match(utr):
        raise PayoutError(_('Enter the UPI transaction reference (UTR) - 6 to 30 letters/digits, no spaces.'))

    payouts = list(
        Payout.objects.select_for_update().select_related('seller').filter(pk__in=payout_ids).order_by('pk')
    )
    if not payouts or len(payouts) != len(set(payout_ids)):
        raise PayoutError(_('Some of the selected payouts no longer exist.'))
    if len({p.seller_id for p in payouts}) != 1:
        raise PayoutError(_('One UPI transfer can only cover one seller. Select payouts for a single seller.'))
    not_pending = [p for p in payouts if p.status != 'pending']
    if not_pending:
        raise PayoutError(
            _('%(count)s of the selected payouts are not pending (already paid, on hold or cancelled).') % {'count': len(not_pending)}
        )

    seller = payouts[0].seller
    upi_id = get_upi_id(seller)
    if not upi_id:
        raise PayoutError(_('%(username)s has not added a UPI ID yet.') % {'username': seller.username})
    left = upi_hold_remaining(PayoutProfile.objects.filter(user=seller).first())
    if left:
        raise PayoutError(
            _('%(username)s changed their UPI ID recently, so payouts are on a security hold for another %(duration)s. Contact the seller on their saved phone/email to confirm, or use "Override hold" with a reason.') % {'username': seller.username, 'duration': format_hold(left)}
        )
    if needs_name_check(seller, upi_id):
        raise PayoutError(
            _('Check the UPI name for %(username)s before the first payout to %(upi_id)s: open the payout, enter the name your UPI app shows, and record it.') % {'username': seller.username, 'upi_id': upi_id}
        )

    # The same UTR on two *different* sellers is almost certainly a copy-paste
    # slip. The same seller is fine: one transfer can settle several orders.
    if Payout.objects.filter(utr__iexact=utr, status='paid').exclude(seller=seller).exists():
        raise PayoutError(_('That UTR is already recorded against a different seller. Check the reference.'))

    now = timezone.now()
    for p in payouts:
        p.status = 'paid'
        p.paid_at = now
        p.paid_to_upi = upi_id
        p.utr = utr
        p.paid_by = actor
        p.save(update_fields=['status', 'paid_at', 'paid_to_upi', 'utr', 'paid_by', 'updated_at'])
    return payouts


def _transition(payout_id, *, allowed_from, to, note=''):
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(pk=payout_id)
        if payout.status not in allowed_from:
            raise PayoutError(_("A %(status)s payout can't be changed this way.") % {'status': payout.get_status_display().lower()})
        payout.status = to
        if note:
            payout.note = (payout.note + '\n' if payout.note else '') + note
        payout.save(update_fields=['status', 'note', 'updated_at'])
        return payout


def put_on_hold(payout_id, *, note):
    if not (note or '').strip():
        raise PayoutError(_('Add a note saying why this payout is on hold.'))
    return _transition(payout_id, allowed_from=('pending',), to='on_hold', note=f'Held: {note.strip()}')


def release_hold(payout_id, *, note=''):
    return _transition(payout_id, allowed_from=('on_hold',), to='pending', note=f'Hold released. {note.strip()}'.strip())


def cancel(payout_id, *, note):
    if not (note or '').strip():
        raise PayoutError(_('Add a note saying why this payout is cancelled.'))
    return _transition(payout_id, allowed_from=('pending', 'on_hold'), to='cancelled', note=f'Cancelled: {note.strip()}')


@transaction.atomic
def void_for_refund(order):
    """
    Call when an order's payment is refunded to the buyer. If the seller hasn't
    been paid yet the payout is cancelled; if they already have been, it's
    flagged so staff can recover the money - the sale no longer happened.
    """
    payout = Payout.objects.select_for_update().filter(order=order).first()
    if not payout:
        return None
    if payout.status in ('pending', 'on_hold'):
        payout.status = 'cancelled'
        payout.note = (payout.note + '\n' if payout.note else '') + 'Cancelled automatically: order refunded to buyer.'
        payout.save(update_fields=['status', 'note', 'updated_at'])
    elif payout.status == 'paid' and not payout.refunded_after_payout:
        payout.refunded_after_payout = True
        payout.save(update_fields=['refunded_after_payout', 'updated_at'])
    return payout


@transaction.atomic
def apply_partial_refund(order, amount, *, deduct, note=''):
    """
    Call when the buyer is refunded *part* of an order (the sale still stands).
    `deduct=True` makes the seller bear it: a pending/on-hold payout is reduced
    by `amount` (never below zero), and an already-paid one is flagged so staff
    can recover it. `deduct=False` means BookBridge bears the refund and the
    payout is left alone. Either way the reason is written on the payout.
    """
    payout = Payout.objects.select_for_update().filter(order=order).first()
    if not payout:
        return None
    amount = Decimal(amount)
    label = f'Partial refund of Rs. {amount} to buyer ({note})' if note else f'Partial refund of Rs. {amount} to buyer'
    if not deduct:
        payout.note = (payout.note + '\n' if payout.note else '') + f'{label} - paid by BookBridge, no deduction.'
        payout.save(update_fields=['note', 'updated_at'])
    elif payout.status in ('pending', 'on_hold'):
        taken = min(amount, payout.net_amount)
        payout.net_amount = payout.net_amount - taken
        payout.note = (payout.note + '\n' if payout.note else '') + f'{label} - Rs. {taken} deducted from this payout.'
        payout.save(update_fields=['net_amount', 'note', 'updated_at'])
    elif payout.status == 'paid':
        payout.refunded_after_payout = True
        payout.note = (payout.note + '\n' if payout.note else '') + f'{label} - seller already paid, Rs. {amount} to recover.'
        payout.save(update_fields=['refunded_after_payout', 'note', 'updated_at'])
    return payout


def seller_totals(seller):
    """Headline numbers for the seller's payout page (cancelled rows excluded)."""
    live = Payout.objects.filter(seller=seller).exclude(status='cancelled')
    agg = live.aggregate(
        pending=Sum('net_amount', filter=Q(status='pending')),
        on_hold=Sum('net_amount', filter=Q(status='on_hold')),
        paid=Sum('net_amount', filter=Q(status='paid')),
        commission=Sum('commission_amount'),
        gross=Sum('gross_amount'),
        orders=Count('id'),
    )
    return {k: (v or ZERO) for k, v in agg.items()}
