"""
Exactly-once refunds.

    record = claim_refund(order, amount, 'original', 'return_request', actor=staff)   # raises RefundAlreadyIssued
    ... move the money (Razorpay call, or wallet credit with an idempotency key) ...
    complete_refund(record, reference)        # success
    abandon_refund(record)                    # the money definitely did NOT move (gateway rejected it)

The claim is the guard: it creates the order's single RefundRecord row under a row lock, and the
unique constraint turns any race into RefundAlreadyIssued for the loser. A claim whose outcome is
unknown (process died after Razorpay accepted the refund) stays 'pending' and blocks further
refunds until a finance user checks Razorpay and resolves it in the Django admin - the safe
direction for money: it can fail to refund, never refund twice.
"""
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Order, RefundRecord


class RefundAlreadyIssued(Exception):
    """This order already has a refund (done, or claimed and awaiting confirmation)."""


def claim_refund(order, amount, destination, source, actor=None):
    try:
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=order.pk)  # serialise claims for this order
            if RefundRecord.objects.filter(order_id=order.pk).exists():
                raise RefundAlreadyIssued(str(order.order_id))
            return RefundRecord.objects.create(
                order=order, order_ref=str(order.order_id), amount=amount, destination=destination,
                source=source, actor=actor if getattr(actor, 'pk', None) else None,
            )
    except IntegrityError as exc:  # lost a race that the lock didn't serialise (e.g. SQLite)
        raise RefundAlreadyIssued(str(order.order_id)) from exc


def complete_refund(record, reference=''):
    record.status = 'done'
    record.reference = str(reference or '')[:100]
    record.completed_at = timezone.now()
    record.save(update_fields=['status', 'reference', 'completed_at'])
    return record


def abandon_refund(record):
    """Release a claim after a definite failure so the refund can be attempted again."""
    if record.status == 'pending':
        record.delete()


def has_refund(order):
    return RefundRecord.objects.filter(order_id=order.pk).exists()
