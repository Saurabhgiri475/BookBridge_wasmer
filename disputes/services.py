"""
Dispute business logic. Views call these; nothing else changes a Dispute's
status or moves refund money, so the rules live in one place.

Money rules (the part worth reading twice):
  * A refund is only issued for an order the buyer actually paid for
    (payment escrow_held / released) and never twice - `order_already_refunded`
    is also checked by the older ReturnRequest admin actions.
  * A refund is capped at what the buyer paid (Order.total_amount).
  * Refunding at least the sale price reverses the sale: the seller's payout is
    cancelled (or flagged for recovery if already paid) exactly as for a return.
    A smaller refund is a partial one: staff choose whether the seller bears it
    (deducted from a not-yet-paid payout / flagged if already paid) or BookBridge does.
  * While a dispute is active the seller's pending payout is put on hold, and
    only released again if this dispute is what held it.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.utils import timezone

from orders import payments as razorpay_gateway
from orders.refund_ledger import RefundAlreadyIssued, abandon_refund, claim_refund, complete_refund, has_refund
from payouts import services as payout_services
from wallet.services import credit as wallet_credit

from .models import ACTIVE_STATUSES, Dispute, DisputeEvidence, DisputeMessage
from django.utils.translation import gettext as _

# --- Policy (business choices, kept in one place) ---------------------------
DISPUTE_WINDOW_DAYS = 30          # after delivery, buyers can open a dispute
NOT_RECEIVED_AFTER_DAYS = 7       # in transit this long -> "not received" can be raised
SELLER_RESPONSE_DAYS = 3          # seller's window to reply before the buyer may escalate (and auto-escalation)
MAX_EVIDENCE_PER_DISPUTE = 15

PRE_DELIVERY_STATUSES = ('shipped', 'out_for_delivery')
DISPUTABLE_STATUSES = PRE_DELIVERY_STATUSES + ('delivered',)
PAID_PAYMENT_STATUSES = ('escrow_held', 'released')

CENT = Decimal('0.01')


class DisputeError(Exception):
    """Something that can't be done - the message is shown to the user."""


def _money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# --- Eligibility -------------------------------------------------------------

def delivered_at(order):
    return order.delivered_at or order.updated_at


def allowed_categories(order):
    """Which complaint types make sense for this order right now."""
    if order.status in PRE_DELIVERY_STATUSES:
        return ['not_received']
    keys = [k for k, _ in Dispute.CATEGORY_CHOICES]
    if order.is_digital:
        return [k for k in keys if k in ('not_as_described', 'digital_file', 'other')]
    return [k for k in keys if k != 'digital_file']


def order_already_refunded(order):
    """True if the buyer has already been refunded for this order, by a return
    refund or a dispute - used to stop a second refund from the other route."""
    payment = getattr(order, 'payment', None)
    if payment and payment.status == 'refunded':
        return True
    return has_refund(order) or Dispute.objects.filter(order=order, outcome='refund', refund_amount__gt=0).exists()


def blocked_reason(order):
    """Why a dispute can't be opened on `order` now, or '' if it can."""
    if order.status not in DISPUTABLE_STATUSES:
        return _('Disputes can be opened once an order has shipped.')
    if order.disputes.exclude(status='withdrawn').exists():
        return _('A dispute has already been opened for this order.')
    if order_already_refunded(order):
        return _('This order has already been refunded.')
    now = timezone.now()
    if order.status in PRE_DELIVERY_STATUSES:
        if now - order.created_at < timedelta(days=NOT_RECEIVED_AFTER_DAYS):
            return (
                _('Orders can be reported as not received %(days)s days after they were placed. Track the shipment from the order page until then.') % {'days': NOT_RECEIVED_AFTER_DAYS}
            )
    elif now - delivered_at(order) > timedelta(days=DISPUTE_WINDOW_DAYS):
        return _('The %(days)s-day window to open a dispute after delivery has passed.') % {'days': DISPUTE_WINDOW_DAYS}
    return ''


# --- Internals ----------------------------------------------------------------

def _locked(dispute_id):
    return Dispute.objects.select_for_update().select_related('order', 'buyer', 'seller').get(pk=dispute_id)


def _system_message(dispute, body, internal=False):
    return DisputeMessage.objects.create(dispute=dispute, role='system', body=body, internal=internal)


def _attach(dispute, message, user, files):
    files = list(files or [])
    if not files:
        return []
    if dispute.evidence.count() + len(files) > MAX_EVIDENCE_PER_DISPUTE:
        raise DisputeError(_('A dispute can hold at most %(max_photos)s photos.') % {'max_photos': MAX_EVIDENCE_PER_DISPUTE})
    return [DisputeEvidence.objects.create(dispute=dispute, message=message, uploaded_by=user, file=f) for f in files]


def _role_of(dispute, user):
    if user.pk == dispute.buyer_id:
        return 'buyer'
    if user.pk == dispute.seller_id:
        return 'seller'
    if user.is_staff:
        return 'staff'
    return None


def _hold_payout(dispute):
    """Hold the seller's pending payout while the dispute is open."""
    payout = getattr(dispute.order, 'payout', None) if dispute.order_id else None
    if not payout or dispute.payout_held:
        return
    try:
        payout_services.put_on_hold(payout.pk, note=f'Dispute {dispute.reference} opened.')
    except payout_services.PayoutError:
        return  # already paid / on hold / cancelled - nothing to hold
    dispute.payout_held = True
    dispute.save(update_fields=['payout_held', 'updated_at'])


def _release_payout_hold(dispute):
    if not dispute.payout_held:
        return
    payout = getattr(dispute.order, 'payout', None) if dispute.order_id else None
    if payout:
        try:
            payout_services.release_hold(payout.pk, note=f'Dispute {dispute.reference} closed.')
        except payout_services.PayoutError:
            pass  # staff already changed it by hand
    dispute.payout_held = False
    dispute.save(update_fields=['payout_held', 'updated_at'])


def hold_payout_for_active_dispute(order, payout):
    """Called by payouts.services.ensure_payout when a payout is created: if the
    buyer disputed the order *before* delivery was confirmed, the brand-new
    payout starts on hold."""
    dispute = Dispute.objects.filter(order=order, status__in=ACTIVE_STATUSES).first()
    if dispute and payout.status == 'pending':
        _hold_payout(Dispute.objects.select_related('order').get(pk=dispute.pk))


# --- Buyer / seller actions ---------------------------------------------------

@transaction.atomic
def open_dispute(order, buyer, *, category, description, desired_resolution, requested_amount=None, files=None):
    from orders.models import Order
    from bookmarket.notify import notify_dispute_opened

    order = Order.objects.select_for_update().select_related('book__seller', 'buyer').get(pk=order.pk)
    if order.buyer_id != buyer.pk:
        raise DisputeError(_('Only the buyer can open a dispute for this order.'))
    reason = blocked_reason(order)
    if reason:
        raise DisputeError(reason)
    if category not in allowed_categories(order):
        raise DisputeError(_('That complaint type does not apply to this order.'))
    description = (description or '').strip()
    if len(description) < 20:
        raise DisputeError(_('Please describe the problem in at least a sentence or two (20+ characters).'))
    if requested_amount is not None:
        requested_amount = _money(requested_amount)
        if requested_amount <= 0 or requested_amount > order.total_amount:
            raise DisputeError(_('The amount must be between ₹0.01 and ₹%(total_amount)s.') % {'total_amount': order.total_amount})

    try:
        with transaction.atomic():
            dispute = Dispute.objects.create(
                order=order, order_ref=str(order.order_id)[:8], book_title=order.book.title[:255],
                buyer=buyer, seller=order.book.seller, category=category, description=description,
                desired_resolution=desired_resolution, requested_amount=requested_amount,
                seller_response_due=timezone.now() + timedelta(days=SELLER_RESPONSE_DAYS),
            )
    except IntegrityError:
        raise DisputeError(_('A dispute is already open for this order.'))

    message = DisputeMessage.objects.create(dispute=dispute, author=buyer, role='buyer', body=description)
    _attach(dispute, message, buyer, files)
    _system_message(
        dispute,
        f'Dispute opened. {dispute.seller.username} has {SELLER_RESPONSE_DAYS} days to reply before it can be '
        'escalated to the BookBridge team.',
    )
    _hold_payout(dispute)
    transaction.on_commit(lambda: notify_dispute_opened(dispute))
    return dispute


@transaction.atomic
def post_message(dispute_id, user, body, files=None):
    """A buyer, seller or staff reply on an active dispute."""
    from bookmarket.notify import notify_dispute_message

    dispute = _locked(dispute_id)
    role = _role_of(dispute, user)
    if role is None:
        raise DisputeError(_('You are not part of this dispute.'))
    if not dispute.is_active:
        raise DisputeError(_('This dispute is closed - no more replies can be added.'))
    body = (body or '').strip()
    if len(body) < 2:
        raise DisputeError(_('Write a message first.'))
    message = DisputeMessage.objects.create(dispute=dispute, author=user, role=role, body=body)
    _attach(dispute, message, user, files)
    if role == 'seller' and dispute.status == 'open':
        dispute.status = 'seller_responded'
        dispute.save(update_fields=['status', 'updated_at'])
    else:
        dispute.save(update_fields=['updated_at'])
    transaction.on_commit(lambda: notify_dispute_message(dispute, message))
    return message


@transaction.atomic
def seller_agree(dispute_id, seller, body=''):
    """The seller accepts the buyer is owed a refund. Staff still issue it (the
    money moves from one place only), so this sends the dispute straight to them."""
    from bookmarket.notify import notify_dispute_escalated

    dispute = _locked(dispute_id)
    if seller.pk != dispute.seller_id:
        raise DisputeError(_('Only the seller can do this.'))
    if dispute.status not in ('open', 'seller_responded'):
        raise DisputeError(_('This dispute can no longer be changed this way.'))
    body = (body or '').strip()
    if body:
        DisputeMessage.objects.create(dispute=dispute, author=seller, role='seller', body=body)
    dispute.seller_agreed = True
    dispute.status = 'escalated'
    dispute.escalated_at = timezone.now()
    dispute.save(update_fields=['seller_agreed', 'status', 'escalated_at', 'updated_at'])
    _system_message(
        dispute,
        f'{seller.username} agrees the buyer should be refunded. BookBridge will now issue the refund.',
    )
    transaction.on_commit(lambda: notify_dispute_escalated(dispute))
    return dispute


@transaction.atomic
def escalate(dispute_id, buyer, reason=''):
    from bookmarket.notify import notify_dispute_escalated

    dispute = _locked(dispute_id)
    if buyer.pk != dispute.buyer_id:
        raise DisputeError(_('Only the buyer can escalate this dispute.'))
    if not dispute.buyer_can_escalate:
        if dispute.status == 'open':
            raise DisputeError(_('Please give the seller their full reply window first.'))
        raise DisputeError(_('This dispute can no longer be escalated.'))
    dispute.status = 'escalated'
    dispute.escalated_at = timezone.now()
    dispute.save(update_fields=['status', 'escalated_at', 'updated_at'])
    reason = (reason or '').strip()
    _system_message(dispute, 'Escalated to the BookBridge team by the buyer.' + (f' Reason: {reason}' if reason else ''))
    transaction.on_commit(lambda: notify_dispute_escalated(dispute))
    return dispute


@transaction.atomic
def withdraw(dispute_id, buyer):
    from bookmarket.notify import notify_dispute_withdrawn

    dispute = _locked(dispute_id)
    if buyer.pk != dispute.buyer_id:
        raise DisputeError(_('Only the buyer can withdraw this dispute.'))
    if not dispute.is_active:
        raise DisputeError(_('This dispute is already closed.'))
    dispute.status = 'withdrawn'
    dispute.save(update_fields=['status', 'updated_at'])
    _release_payout_hold(dispute)
    _system_message(dispute, 'The buyer withdrew this dispute.')
    transaction.on_commit(lambda: notify_dispute_withdrawn(dispute))
    return dispute


def escalate_overdue(now=None):
    """Escalate disputes where the seller never replied inside their window.
    Run from cron via `manage.py escalate_overdue_disputes`; safe to run often."""
    from bookmarket.notify import notify_dispute_escalated

    now = now or timezone.now()
    ids = list(Dispute.objects.filter(status='open', seller_response_due__lte=now).values_list('pk', flat=True))
    done = 0
    for pk in ids:
        with transaction.atomic():
            dispute = _locked(pk)
            if dispute.status != 'open':
                continue
            dispute.status = 'escalated'
            dispute.escalated_at = now
            dispute.auto_escalated = True
            dispute.save(update_fields=['status', 'escalated_at', 'auto_escalated', 'updated_at'])
            _system_message(dispute, 'The seller did not reply in time, so this dispute was escalated to the BookBridge team.')
            transaction.on_commit(lambda d=dispute: notify_dispute_escalated(d))
            done += 1
    return done


# --- Staff actions ------------------------------------------------------------

@transaction.atomic
def staff_note(dispute_id, staff, body):
    dispute = _locked(dispute_id)
    body = (body or '').strip()
    if not body:
        raise DisputeError(_('Write a note first.'))
    return DisputeMessage.objects.create(dispute=dispute, author=staff, role='staff', body=body, internal=True)


def refundable_amount(order):
    """The most a refund can be: what the buyer paid."""
    return _money(order.total_amount)


def _issue_refund(dispute, amount, destination, staff=None):
    """Move the money. Returns a reference string. Raises DisputeError (nothing
    changed) if the refund can't be made."""
    order = dispute.order
    if order is None:
        raise DisputeError(_('The order for this dispute no longer exists.'))
    payment = getattr(order, 'payment', None)
    if not payment or payment.status not in PAID_PAYMENT_STATUSES:
        raise DisputeError(
            _('The buyer has not paid for this order yet (e.g. cash on delivery that was never delivered), '
            'so there is nothing to refund.')
        )
    if order_already_refunded(order):
        raise DisputeError(_('This order has already been refunded.'))
    if amount > refundable_amount(order):
        raise DisputeError(_('The refund cannot be more than the buyer paid (₹%(amount)s).') % {'amount': refundable_amount(order)})

    description = f'Refund for dispute {dispute.reference} - order #{dispute.order_ref} ("{dispute.book_title}")'
    # Claim the order's single refund slot first: a second refund (another click, a return-request
    # refund, a retry) now fails here instead of paying the buyer twice.
    try:
        record = claim_refund(order, amount, destination, 'dispute', actor=staff)
    except RefundAlreadyIssued:
        raise DisputeError(_('This order has already been refunded.'))
    if destination == 'original':
        if not payment.razorpay_payment_id:
            abandon_refund(record)
            raise DisputeError(_('This order has no online payment to refund to - refund to the wallet instead.'))
        try:
            result = razorpay_gateway.refund_payment(
                payment.razorpay_payment_id, amount_rupees=amount,
                notes={'dispute': dispute.reference, 'order_id': str(order.order_id)},
            )
        except Exception as exc:  # gateway/network failure: nothing was refunded
            abandon_refund(record)
            raise DisputeError(_('Razorpay did not accept the refund (%(error_class)s). Nothing was changed - try again or refund to the wallet.') % {'error_class': exc.__class__.__name__})
        reference = str((result or {}).get('id', ''))[:100]
        complete_refund(record, reference)
        return reference
    txn = wallet_credit(order.buyer, amount, 'refund', order=order, description=description[:255],
                        idempotency_key=f'refund:{order.order_id}')
    reference = f'wallet-txn-{txn.pk}' if txn else ''
    complete_refund(record, reference)
    return reference


@transaction.atomic
def staff_resolve(dispute_id, staff, *, outcome, note, refund_amount=None, destination='wallet', deduct_from_seller=True):
    from bookmarket.notify import notify_dispute_resolved

    dispute = _locked(dispute_id)
    if not dispute.is_active:
        raise DisputeError(_('This dispute is already closed.'))
    if outcome not in dict(Dispute.OUTCOME_CHOICES):
        raise DisputeError(_('Choose an outcome.'))
    note = (note or '').strip()
    if not note:
        raise DisputeError(_('Add a short explanation - both the buyer and the seller will see it.'))

    order = dispute.order
    refunded = None
    if outcome == 'refund':
        if refund_amount is None:
            raise DisputeError(_('Enter the refund amount.'))
        refunded = _money(refund_amount)
        if refunded <= 0:
            raise DisputeError(_('The refund amount must be more than zero.'))
        if destination not in dict(Dispute.REFUND_DEST_CHOICES):
            raise DisputeError(_('Choose where the refund goes.'))
        dispute.refund_reference = _issue_refund(dispute, refunded, destination, staff=staff)
        dispute.refund_amount = refunded
        dispute.refund_destination = destination

        payment = getattr(order, 'payment', None)
        sale_reversed = refunded >= order.final_price
        if sale_reversed:
            if payment and payment.status != 'refunded':
                payment.status = 'refunded'
                payment.save(update_fields=['status'])
            payout_services.void_for_refund(order)
            dispute.payout_held = False  # the payout row is now cancelled/flagged; nothing left to release
            rr = getattr(order, 'return_request', None)
            if rr and rr.status != 'refunded':
                rr.status = 'refunded'
                rr.save(update_fields=['status'])
        else:
            payout_services.apply_partial_refund(
                order, refunded, deduct=deduct_from_seller, note=f'Dispute {dispute.reference}',
            )
            dispute.deducted_from_seller = bool(deduct_from_seller)

    dispute.status = 'resolved'
    dispute.outcome = outcome
    dispute.resolution_note = note
    dispute.resolved_by = staff
    dispute.resolved_at = timezone.now()
    dispute.save()
    _release_payout_hold(dispute)

    summary = dict(Dispute.OUTCOME_CHOICES)[outcome]
    if refunded:
        summary += f' - ₹{refunded} to the {dict(Dispute.REFUND_DEST_CHOICES)[destination]}'
    _system_message(dispute, f'Resolved by BookBridge: {summary}.')
    DisputeMessage.objects.create(dispute=dispute, author=staff, role='staff', body=note)
    transaction.on_commit(lambda: notify_dispute_resolved(dispute))
    return dispute


# --- Counts for dashboards ------------------------------------------------------

def staff_attention_count():
    return Dispute.objects.filter(status='escalated').count()
