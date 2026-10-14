import logging

from django.contrib import admin, messages

from . import payments as razorpay_gateway
from adminpanel.models import AuditLog
from django.db import transaction

from .models import Offer, Order, Payment, RefundRecord, Shipment, Review, ReturnRequest
from .refund_ledger import RefundAlreadyIssued, abandon_refund, claim_refund, complete_refund
from bookmarket.notify import notify_refund_issued
from payouts.services import void_for_refund
from wallet.services import credit as wallet_credit
from disputes.services import order_already_refunded

logger = logging.getLogger(__name__)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('order_id', 'book', 'buyer', 'final_price', 'order_type', 'status', 'payment_method', 'created_at')
    list_filter = ('status', 'payment_method', 'order_type')
    search_fields = ('order_id', 'book__title', 'buyer__username')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('order', 'amount', 'method', 'status', 'razorpay_payment_id', 'created_at')
    list_filter = ('status', 'method')
    search_fields = ('razorpay_order_id', 'razorpay_payment_id', 'transaction_ref')


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ('tracking_id', 'order', 'courier_name', 'status', 'tracking_synced_at')
    list_filter = ('status', 'courier_name')
    search_fields = ('tracking_id', 'shiprocket_order_id')


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ('order', 'reason', 'status', 'created_at')
    list_filter = ('status', 'reason')
    actions = ['approve_and_refund', 'refund_to_wallet']

    def has_refund_permission(self, request):
        # Moving money is a Finance capability (adminpanel/roles.py), not something every
        # staff member with "change" rights on return requests may do. Superusers always can.
        return request.user.has_perm('adminpanel.cap_finance')

    @admin.action(description='Approve and issue a real Razorpay refund (skips COD orders - refund those manually)', permissions=['refund'])
    def approve_and_refund(self, request, queryset):
        refunded, skipped, failed = 0, 0, 0
        for rr in queryset.select_related('order__payment'):
            payment = getattr(rr.order, 'payment', None)
            if not payment or not payment.razorpay_payment_id or order_already_refunded(rr.order):
                skipped += 1
                continue
            try:
                record = claim_refund(rr.order, rr.order.final_price, 'original', 'return_request', actor=request.user)
            except RefundAlreadyIssued:  # another click / another staff member got there first
                skipped += 1
                continue
            try:
                result = razorpay_gateway.refund_payment(
                    payment.razorpay_payment_id,
                    amount_rupees=rr.order.final_price,
                    notes={'reason': rr.reason, 'order_id': str(rr.order.order_id)},
                )
            except Exception:
                logger.exception('Refund failed for return request %s', rr.pk)
                abandon_refund(record)  # Razorpay refused: nothing moved, so the claim is released for a retry
                failed += 1
                continue
            complete_refund(record, (result or {}).get('id', ''))
            payment.status = 'refunded'
            payment.save(update_fields=['status'])
            rr.status = 'refunded'
            rr.save(update_fields=['status'])
            void_for_refund(rr.order)
            notify_refund_issued(payment)
            AuditLog.record(actor=request.user, action='refund_issued', target_repr=f'Order {str(rr.order.order_id)[:8]} (Razorpay)',
                            target_id=rr.order.pk, note=f'Rs {rr.order.final_price} to the original payment method.',
                            request=request, old={'refund_status': 'none'},
                            new={'refund_status': 'refunded', 'amount': rr.order.final_price, 'destination': 'original payment method'})
            refunded += 1
        if refunded:
            self.message_user(request, f'{refunded} refund(s) issued via Razorpay.', level=messages.SUCCESS)
        if skipped:
            self.message_user(
                request, f'{skipped} order(s) skipped (COD, no gateway payment, or already refunded - refund COD manually).',
                level=messages.WARNING,
            )
        if failed:
            self.message_user(request, f'{failed} refund(s) failed - check logs.', level=messages.ERROR)

    @admin.action(description='Refund to BookBridge Wallet (works for any payment method, credited instantly)', permissions=['refund'])
    def refund_to_wallet(self, request, queryset):
        refunded, already_done = 0, 0
        for rr in queryset.select_related('order__payment', 'order__buyer'):
            if rr.status == 'refunded' or order_already_refunded(rr.order):
                already_done += 1
                continue
            order = rr.order
            try:
                with transaction.atomic():
                    record = claim_refund(order, order.final_price, 'wallet', 'return_request', actor=request.user)
                    txn = wallet_credit(
                        order.buyer, order.final_price, 'refund', order=order,
                        description=f'Refund for order #{str(order.order_id)[:8]} (\"{order.book.title}\") - {rr.get_reason_display()}',
                        idempotency_key=f'refund:{order.order_id}',
                    )
                    complete_refund(record, f'wallet-txn-{txn.pk}' if txn else '')
            except RefundAlreadyIssued:
                already_done += 1
                continue
            payment = getattr(order, 'payment', None)
            if payment and payment.status != 'refunded':
                payment.status = 'refunded'
                payment.save(update_fields=['status'])
            rr.status = 'refunded'
            rr.save(update_fields=['status'])
            void_for_refund(order)
            AuditLog.record(actor=request.user, action='refund_issued', target_repr=f'Order {str(order.order_id)[:8]} (wallet)',
                            target_id=order.pk, note=f'Rs {order.final_price} to the buyer\'s wallet.',
                            request=request, old={'refund_status': 'none'},
                            new={'refund_status': 'refunded', 'amount': order.final_price, 'destination': 'wallet'})
            refunded += 1
        if refunded:
            self.message_user(request, f'{refunded} refund(s) credited to buyers\' wallets.', level=messages.SUCCESS)
        if already_done:
            self.message_user(request, f'{already_done} return request(s) were already refunded - skipped.', level=messages.WARNING)


@admin.register(RefundRecord)
class RefundRecordAdmin(admin.ModelAdmin):
    """The refund ledger (read-only). Only a stuck 'pending' claim ever needs a human: see orders/refund_ledger.py."""
    list_display = ('order_ref', 'amount', 'destination', 'source', 'status', 'reference', 'actor', 'created_at')
    list_filter = ('status', 'destination', 'source')
    search_fields = ('order_ref', 'reference')
    actions = ['mark_done', 'release_claim']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_refund_permission(self, request):
        return request.user.has_perm('adminpanel.cap_finance')

    @admin.action(description='Pending claim: I confirmed in Razorpay that the money WAS refunded - mark done', permissions=['refund'])
    def mark_done(self, request, queryset):
        n = 0
        for rec in queryset.filter(status='pending'):
            complete_refund(rec, rec.reference or 'confirmed manually')
            AuditLog.record(actor=request.user, action='refund_marked_done', target_repr=f'Order {rec.order_ref[:8]}',
                            target_id=rec.order_id, note='Pending refund confirmed done by finance.',
                            request=request, old={'status': 'pending'}, new={'status': 'done', 'amount': rec.amount})
            n += 1
        self.message_user(request, f'{n} refund claim(s) marked done.', level=messages.SUCCESS if n else messages.WARNING)

    @admin.action(description='Pending claim: I confirmed NOTHING was refunded - release it so the refund can be retried', permissions=['refund'])
    def release_claim(self, request, queryset):
        n = 0
        for rec in queryset.filter(status='pending'):
            AuditLog.record(actor=request.user, action='refund_claim_released', target_repr=f'Order {rec.order_ref[:8]}',
                            target_id=rec.order_id, note='Pending refund claim released by finance (no money moved).',
                            request=request, old={'status': 'pending', 'amount': rec.amount}, new={'status': 'released'})
            abandon_refund(rec)
            n += 1
        self.message_user(request, f'{n} refund claim(s) released.', level=messages.SUCCESS if n else messages.WARNING)


admin.site.register(Offer)
admin.site.register(Review)
