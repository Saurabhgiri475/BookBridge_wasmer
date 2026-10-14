"""While one buyer is on the online-payment page, nobody else can start paying for the same copy."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from books.models import Book, Category

from .models import Order, Payment
from .views import _clear_payment_hold, _mark_book_taken, _reserve_for_payment

CHECKOUT = {
    'delivery_address': '1 MG Road', 'delivery_city': 'Kanpur', 'delivery_pincode': '208001',
    'payment_method': 'upi',
}


class PaymentHoldTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='hs', password='pw')
        self.a = User.objects.create_user(username='ha', password='pw')
        self.b = User.objects.create_user(username='hb', password='pw')
        cat, _ = Category.objects.get_or_create(name='Test Category')
        self.book = Book.objects.create(seller=self.seller, category=cat, title='B', price=300, status='available')

    def _checkout(self, user):
        self.client.force_login(user)
        with mock.patch('orders.views.razorpay_gateway.create_order', return_value={'id': f'order_{user.pk}'}):
            return self.client.post(reverse('orders:checkout', args=[self.book.pk]), CHECKOUT)

    def test_first_buyer_gets_a_hold_and_second_is_turned_away(self):
        self.assertEqual(self._checkout(self.a).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.payment_reserved_by, self.a)
        self.assertTrue(self.book.payment_hold_active())
        self.assertEqual(Order.objects.filter(buyer=self.a).count(), 1)

        self._checkout(self.b)
        self.assertEqual(Order.objects.filter(buyer=self.b).count(), 0)
        self.assertEqual(Payment.objects.count(), 1)
        self.client.force_login(self.b)
        response = self.client.get(reverse('orders:checkout', args=[self.book.pk]))
        self.assertEqual(response.status_code, 302)  # bounced back to the listing

    def test_same_buyer_can_retry_while_holding(self):
        self._checkout(self.a)
        self._checkout(self.a)
        self.assertEqual(Order.objects.filter(buyer=self.a).count(), 2)

    def test_hold_expires_by_itself(self):
        self._checkout(self.a)
        Book.objects.filter(pk=self.book.pk).update(payment_reserved_until=timezone.now() - timedelta(minutes=1))
        self._checkout(self.b)
        self.assertEqual(Order.objects.filter(buyer=self.b).count(), 1)
        self.book.refresh_from_db()
        self.assertEqual(self.book.payment_reserved_by, self.b)

    def test_cod_or_wallet_purchase_cannot_jump_a_live_hold(self):
        self._checkout(self.a)
        self.book.refresh_from_db()
        order_b = Order.objects.create(buyer=self.b, book=self.book, final_price=300)
        self.assertFalse(_mark_book_taken(self.book, order_b))
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'available')

    def test_paying_buyer_takes_the_book_and_the_hold_is_cleared(self):
        self._checkout(self.a)
        self.book.refresh_from_db()
        order_a = Order.objects.get(buyer=self.a)
        self.assertTrue(_mark_book_taken(self.book, order_a))
        self.book.refresh_from_db()
        self.assertEqual((self.book.status, self.book.payment_reserved_by, self.book.payment_reserved_until), ('sold', None, None))

    def test_gateway_failure_releases_the_hold(self):
        self.client.force_login(self.a)
        with mock.patch('orders.views.razorpay_gateway.create_order', side_effect=RuntimeError('down')):
            self.client.post(reverse('orders:checkout', args=[self.book.pk]), CHECKOUT)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.payment_reserved_by)
        self.assertEqual(Order.objects.count(), 0)

    def test_clear_only_removes_the_owners_hold(self):
        self.assertTrue(_reserve_for_payment(self.book, self.a))
        _clear_payment_hold(self.book, self.b)  # not the owner: no effect
        self.book.refresh_from_db()
        self.assertEqual(self.book.payment_reserved_by, self.a)
        _clear_payment_hold(self.book, self.a)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.payment_reserved_by)
