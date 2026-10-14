from decimal import Decimal
from unittest import mock

from django.test import TestCase, RequestFactory
from django.urls import reverse

from accounts.models import User
from books.models import Book, Category
from bookmarket.security import safe_next_url
from . import payments as gateway
from .models import Order, Payment


def _rp_payment(order_id='order_A', amount=104000, currency='INR', status='captured'):
    return {'id': 'pay_1', 'order_id': order_id, 'amount': amount, 'currency': currency, 'status': status}


class PaymentCallbackVerificationTests(TestCase):
    """A valid Razorpay signature isn't enough: the callback must also be for
    THIS order, with the right amount, and actually captured."""

    def setUp(self):
        seller = User.objects.create_user(username='sel_pc', password='pw')
        self.buyer = User.objects.create_user(username='buy_pc', password='pw')
        cat, _ = Category.objects.get_or_create(name='Test Category')
        book = Book.objects.create(seller=seller, category=cat, title='B', price=1000, status='available')
        self.order = Order.objects.create(
            buyer=self.buyer, book=book, final_price=1000, delivery_charge=40,
            payment_method='upi', status='pending',
            delivery_address='1 MG Road', delivery_city='Kanpur', delivery_pincode='208001',
        )
        self.payment = Payment.objects.create(
            order=self.order, amount=self.order.total_amount, method='upi',
            status='created', razorpay_order_id='order_A',
        )
        self.client.login(username='buy_pc', password='pw')
        self.url = reverse('orders:payment_callback', args=[self.order.order_id])

    def _post(self, order_id='order_A'):
        return self.client.post(self.url, {
            'razorpay_order_id': order_id, 'razorpay_payment_id': 'pay_1', 'razorpay_signature': 'sig',
        })

    @mock.patch('orders.views._finalize_paid_order')
    @mock.patch('orders.payments.verify_payment_signature')
    def test_order_id_from_a_different_checkout_is_rejected(self, sig, finalize):
        # Signature would pass (it's a real triple for some other order)...
        self._post(order_id='order_OTHER')
        finalize.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'created')  # a forged callback can't cancel it either

    @mock.patch('orders.views._finalize_paid_order')
    @mock.patch('orders.payments.fetch_payment', return_value=_rp_payment(amount=100))
    @mock.patch('orders.payments.verify_payment_signature')
    def test_underpayment_is_rejected(self, sig, fetch, finalize):
        self._post()
        finalize.assert_not_called()

    @mock.patch('orders.views._finalize_paid_order')
    @mock.patch('orders.payments.fetch_payment', return_value=_rp_payment(status='failed'))
    @mock.patch('orders.payments.verify_payment_signature')
    def test_uncaptured_failed_payment_is_rejected(self, sig, fetch, finalize):
        self._post()
        finalize.assert_not_called()

    @mock.patch('orders.views._finalize_paid_order')
    @mock.patch('orders.payments.fetch_payment', return_value=_rp_payment(status='authorized'))
    @mock.patch('orders.payments.verify_payment_signature')
    def test_authorized_waits_for_webhook(self, sig, fetch, finalize):
        self._post()
        finalize.assert_not_called()

    @mock.patch('orders.views._finalize_paid_order')
    @mock.patch('orders.payments.fetch_payment', side_effect=RuntimeError('network'))
    @mock.patch('orders.payments.verify_payment_signature')
    def test_razorpay_api_error_fails_closed(self, sig, fetch, finalize):
        self._post()
        finalize.assert_not_called()

    @mock.patch('orders.views._finalize_paid_order')
    @mock.patch('orders.payments.fetch_payment', return_value=_rp_payment())
    @mock.patch('orders.payments.verify_payment_signature')
    def test_genuine_payment_is_finalized(self, sig, fetch, finalize):
        self._post()
        finalize.assert_called_once()


class WebhookPayloadCheckTests(TestCase):
    def test_amount_currency_and_order_must_match(self):
        p = Payment(amount=Decimal('1040.00'), razorpay_order_id='order_A')
        self.assertTrue(gateway.check_payment_against_payment_row(_rp_payment(), p)[0])
        self.assertFalse(gateway.check_payment_against_payment_row(_rp_payment(amount=1), p)[0])
        self.assertFalse(gateway.check_payment_against_payment_row(_rp_payment(currency='USD'), p)[0])
        self.assertFalse(gateway.check_payment_against_payment_row(_rp_payment(order_id='x'), p)[0])


class SafeNextUrlTests(TestCase):
    def _url(self, nxt, secure=False):
        req = RequestFactory().post('/x/', {'next': nxt}, secure=secure)
        return safe_next_url(req, 'books:list')

    def test_local_paths_are_kept(self):
        self.assertEqual(self._url('/books/?page=2'), '/books/?page=2')

    def test_external_and_scheme_relative_urls_are_dropped(self):
        default = reverse('books:list')
        for bad in ('https://evil.example/', '//evil.example', 'javascript:alert(1)', 'http://testserver.evil.com'):
            self.assertEqual(self._url(bad), default, bad)

    def test_missing_next_uses_default(self):
        self.assertEqual(self._url(''), reverse('books:list'))
