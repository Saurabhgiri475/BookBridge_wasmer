from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.test import RequestFactory, TestCase

from accounts.models import User
from adminpanel.models import AuditLog
from coupons.models import Coupon, CouponRedemption
from coupons.services import CouponError, redeem_coupon
from disputes import services as dispute_services
from disputes.tests import DisputeTestBase, make_order
from orders.models import Order, RefundRecord, ReturnRequest
from orders.refund_ledger import RefundAlreadyIssued, abandon_refund, claim_refund, complete_refund
from wallet.models import WalletTransaction
from wallet.services import (IdempotencyConflict, InsufficientBalance, award_delivery_cashback, credit, debit,
                             get_wallet)

PW = 'Str0ng!Pass#123'


class WalletIdempotencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('walletuser', 'w@x.com', PW)

    def test_repeating_a_credit_with_the_same_key_pays_once(self):
        a = credit(self.user, 100, 'refund', idempotency_key='k1')
        b = credit(self.user, 100, 'refund', idempotency_key='k1')
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(get_wallet(self.user).balance, Decimal('100.00'))
        self.assertEqual(WalletTransaction.objects.count(), 1)

    def test_repeating_a_debit_with_the_same_key_charges_once(self):
        credit(self.user, 500, 'refund')
        a = debit(self.user, 120, 'payment', idempotency_key='pay-1')
        b = debit(self.user, 120, 'payment', idempotency_key='pay-1')
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(get_wallet(self.user).balance, Decimal('380.00'))

    def test_a_repeated_debit_still_succeeds_when_the_balance_has_since_dropped(self):
        credit(self.user, 100, 'refund')
        debit(self.user, 100, 'payment', idempotency_key='pay-2')
        self.assertEqual(debit(self.user, 100, 'payment', idempotency_key='pay-2').amount, Decimal('100.00'))  # no error, no 2nd charge
        self.assertEqual(get_wallet(self.user).balance, Decimal('0.00'))

    def test_without_a_key_every_call_is_a_new_transaction(self):
        credit(self.user, 10, 'refund')
        credit(self.user, 10, 'refund')
        self.assertEqual(get_wallet(self.user).balance, Decimal('20.00'))

    def test_reusing_a_key_for_something_different_is_rejected(self):
        credit(self.user, 100, 'refund', idempotency_key='k2')
        with self.assertRaises(IdempotencyConflict):
            credit(self.user, 999, 'refund', idempotency_key='k2')       # other amount
        with self.assertRaises(IdempotencyConflict):
            debit(self.user, 100, 'payment', idempotency_key='k2')       # other direction
        other = User.objects.create_user('someoneelse', 's@x.com', PW)
        with self.assertRaises(IdempotencyConflict):
            credit(other, 100, 'refund', idempotency_key='k2')           # other wallet
        self.assertEqual(get_wallet(self.user).balance, Decimal('100.00'))

    def test_failed_debit_does_not_consume_the_key(self):
        with self.assertRaises(InsufficientBalance):
            debit(self.user, 50, 'payment', idempotency_key='pay-3')
        credit(self.user, 80, 'refund')
        debit(self.user, 50, 'payment', idempotency_key='pay-3')
        self.assertEqual(get_wallet(self.user).balance, Decimal('30.00'))

    def test_delivery_cashback_is_awarded_once_per_order(self):
        seller = User.objects.create_user('sellr', 'sl@x.com', PW)
        order = make_order(seller, self.user, price=1000)
        award_delivery_cashback(order)
        award_delivery_cashback(order)
        self.assertEqual(WalletTransaction.objects.filter(source='cashback', order=order).count(), 1)
        self.assertEqual(get_wallet(self.user).balance, Decimal('20.00'))


class RefundLedgerTests(DisputeTestBase):
    def setUp(self):
        super().setUp()
        self.order = self.paid_order(price=500)

    def test_second_claim_for_the_same_order_is_refused(self):
        claim_refund(self.order, Decimal('500'), 'wallet', 'return_request', actor=self.staff)
        with self.assertRaises(RefundAlreadyIssued):
            claim_refund(self.order, Decimal('500'), 'original', 'dispute', actor=self.staff)
        self.assertEqual(RefundRecord.objects.count(), 1)

    def test_abandoned_claim_can_be_retried_but_completed_one_cannot(self):
        rec = claim_refund(self.order, Decimal('500'), 'original', 'return_request')
        abandon_refund(rec)
        rec2 = claim_refund(self.order, Decimal('500'), 'original', 'return_request')
        complete_refund(rec2, 'rfnd_123')
        abandon_refund(rec2)                                    # a completed refund can never be released
        self.assertEqual(RefundRecord.objects.get().status, 'done')
        with self.assertRaises(RefundAlreadyIssued):
            claim_refund(self.order, Decimal('500'), 'original', 'return_request')

    def test_order_already_refunded_sees_claims(self):
        self.assertFalse(dispute_services.order_already_refunded(self.order))
        claim_refund(self.order, Decimal('500'), 'wallet', 'dispute')
        self.assertTrue(dispute_services.order_already_refunded(Order.objects.get(pk=self.order.pk)))

    def _action(self, name, queryset, user):
        from orders.models import ReturnRequest as RR
        model_admin = admin.site._registry[RR]
        request = RequestFactory().post('/admin/orders/returnrequest/')
        request.user = user
        request.session, request._messages = mock.MagicMock(), mock.MagicMock()
        getattr(model_admin, name)(request, queryset)

    def test_admin_wallet_refund_run_twice_credits_once_and_is_logged(self):
        rr = ReturnRequest.objects.create(order=self.order, reason='damaged')
        qs = ReturnRequest.objects.filter(pk=rr.pk)
        self._action('refund_to_wallet', qs, self.staff)
        self._action('refund_to_wallet', ReturnRequest.objects.filter(pk=rr.pk), self.staff)
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('500.00'))
        self.assertEqual(WalletTransaction.objects.filter(source='refund', order=self.order).count(), 1)
        record = RefundRecord.objects.get()
        self.assertEqual((record.status, record.destination, record.source), ('done', 'wallet', 'return_request'))
        self.assertEqual(AuditLog.objects.filter(action='refund_issued').count(), 1)

    def test_dispute_refund_after_admin_refund_is_refused(self):
        rr = ReturnRequest.objects.create(order=self.order, reason='damaged')
        d = self.open(self.order)
        self._action('refund_to_wallet', ReturnRequest.objects.filter(pk=rr.pk), self.staff)
        with self.assertRaises(dispute_services.DisputeError):
            dispute_services.staff_resolve(d.pk, self.staff, outcome='refund', note='x', refund_amount=Decimal('100'), destination='wallet')
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('500.00'))

    def test_admin_razorpay_refund_run_twice_calls_the_gateway_once(self):
        order = make_order(self.seller, self.buyer, price=300, razorpay_id='pay_ABC')
        rr = ReturnRequest.objects.create(order=order, reason='damaged')
        with mock.patch('orders.admin.razorpay_gateway.refund_payment', return_value={'id': 'rfnd_1'}) as gw:
            self._action('approve_and_refund', ReturnRequest.objects.filter(pk=rr.pk), self.staff)
            self._action('approve_and_refund', ReturnRequest.objects.filter(pk=rr.pk), self.staff)
        self.assertEqual(gw.call_count, 1)
        record = RefundRecord.objects.get(order=order)
        self.assertEqual((record.status, record.reference), ('done', 'rfnd_1'))

    def test_gateway_failure_releases_the_claim_so_it_can_be_retried(self):
        order = make_order(self.seller, self.buyer, price=300, razorpay_id='pay_DEF')
        rr = ReturnRequest.objects.create(order=order, reason='damaged')
        with mock.patch('orders.admin.razorpay_gateway.refund_payment', side_effect=RuntimeError('timeout')):
            self._action('approve_and_refund', ReturnRequest.objects.filter(pk=rr.pk), self.staff)
        self.assertFalse(RefundRecord.objects.filter(order=order).exists())
        with mock.patch('orders.admin.razorpay_gateway.refund_payment', return_value={'id': 'rfnd_2'}) as gw:
            self._action('approve_and_refund', ReturnRequest.objects.filter(pk=rr.pk), self.staff)
        self.assertEqual(gw.call_count, 1)

    def test_dispute_original_refund_records_the_razorpay_reference(self):
        order = make_order(self.seller, self.buyer, price=300, razorpay_id='pay_GHI')
        payout_order = Order.objects.get(pk=order.pk)
        from payouts import services as payout_services
        payout_services.ensure_payout(payout_order)
        d = self.open(payout_order)
        with mock.patch('disputes.services.razorpay_gateway.refund_payment', return_value={'id': 'rfnd_9'}):
            dispute_services.staff_resolve(d.pk, self.staff, outcome='refund', note='ok', refund_amount=Decimal('300'), destination='original')
        record = RefundRecord.objects.get(order=order)
        self.assertEqual((record.status, record.reference, record.source, record.destination), ('done', 'rfnd_9', 'dispute', 'original'))

    def test_dispute_gateway_failure_leaves_no_claim_behind(self):
        order = make_order(self.seller, self.buyer, price=300, razorpay_id='pay_JKL')
        from payouts import services as payout_services
        payout_services.ensure_payout(Order.objects.get(pk=order.pk))
        d = self.open(Order.objects.get(pk=order.pk))
        with mock.patch('disputes.services.razorpay_gateway.refund_payment', side_effect=RuntimeError('down')):
            with self.assertRaises(dispute_services.DisputeError):
                dispute_services.staff_resolve(d.pk, self.staff, outcome='refund', note='ok', refund_amount=Decimal('300'), destination='original')
        self.assertFalse(RefundRecord.objects.filter(order=order).exists())


class CouponRedemptionIdempotencyTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('cs', 'cs@x.com', PW)
        self.a = User.objects.create_user('ca', 'ca@x.com', PW)
        self.b = User.objects.create_user('cb', 'cb@x.com', PW)
        self.coupon = Coupon.objects.create(code='ONCE', discount_type='flat', discount_value=50, usage_limit_total=1, usage_limit_per_user=1)

    def test_redeeming_the_same_order_twice_returns_the_same_row(self):
        order = make_order(self.seller, self.a)
        r1 = redeem_coupon(self.coupon, self.a, order, Decimal('50'))
        r2 = redeem_coupon(self.coupon, self.a, order, Decimal('50'))
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(CouponRedemption.objects.count(), 1)

    def test_total_limit_is_rechecked_at_redemption_time(self):
        redeem_coupon(self.coupon, self.a, make_order(self.seller, self.a), Decimal('50'))
        with self.assertRaises(CouponError):  # b passed validate_coupon earlier, but the last use is gone now
            redeem_coupon(self.coupon, self.b, make_order(self.seller, self.b), Decimal('50'))
        self.assertEqual(CouponRedemption.objects.count(), 1)

    def test_per_user_limit_is_rechecked_at_redemption_time(self):
        self.coupon.usage_limit_total = None
        self.coupon.save()
        redeem_coupon(self.coupon, self.a, make_order(self.seller, self.a), Decimal('50'))
        with self.assertRaises(CouponError):
            redeem_coupon(self.coupon, self.a, make_order(self.seller, self.a), Decimal('50'))
