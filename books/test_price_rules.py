"""A listing, offer or order can never be free or negative - enforced by the form, the model
validators, the database constraints and the checkout itself (not by HTML min= attributes)."""
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from orders.models import Offer, Order

from .forms import BookForm
from .models import Book, Category
from .fraud_detection import check_suspicious_price


class PriceRuleTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='pseller', password='pw')
        self.buyer = User.objects.create_user(username='pbuyer', password='pw')
        self.cat, _ = Category.objects.get_or_create(name='Test Category')

    def _book(self, **kw):
        data = dict(seller=self.seller, category=self.cat, title='B', price=Decimal('100'), status='available')
        data.update(kw)
        return Book(**data)

    def test_model_validation_rejects_zero_negative_and_sub_rupee_prices(self):
        for bad in ('0', '0.00', '-5', '0.99'):
            with self.assertRaises(ValidationError, msg=bad):
                self._book(price=Decimal(bad)).full_clean()
        self._book(price=Decimal('1.00')).full_clean()  # the minimum is allowed

    def test_model_validation_rejects_bad_quantity_and_original_price(self):
        with self.assertRaises(ValidationError):
            self._book(quantity=0).full_clean()
        with self.assertRaises(ValidationError):
            self._book(original_price=Decimal('-1')).full_clean()

    def test_database_constraint_blocks_zero_price_even_if_validators_are_skipped(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Book.objects.create(seller=self.seller, category=self.cat, title='Free', price=Decimal('0'))
        book = Book.objects.create(seller=self.seller, category=self.cat, title='Ok', price=Decimal('50'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Book.objects.filter(pk=book.pk).update(price=Decimal('-10'))

    def test_form_rejects_a_direct_post_with_zero_price(self):
        form = BookForm(data={
            'listing_type': 'physical', 'title': 'T', 'author': 'A', 'condition': 'good', 'language': 'English',
            'price': '0', 'quantity': '1',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('price', form.errors)

    def test_offer_below_one_rupee_is_rejected(self):
        book = Book.objects.create(seller=self.seller, category=self.cat, title='B', price=Decimal('100'))
        self.client.login(username='pbuyer', password='pw')
        for bad in ('0', '-20', '0.50'):
            self.client.post(reverse('orders:make_offer', args=[book.pk]), {'amount': bad})
        self.assertEqual(Offer.objects.count(), 0)
        self.client.post(reverse('orders:make_offer', args=[book.pk]), {'amount': '80'})
        self.assertEqual(Offer.objects.count(), 1)

    def test_checkout_refuses_an_invalid_price_even_if_one_got_through(self):
        book = Book.objects.create(seller=self.seller, category=self.cat, title='B', price=Decimal('100'))
        self.client.login(username='pbuyer', password='pw')
        with mock.patch('orders.views._resolve_final_price', return_value=(Decimal('0'), None)):
            self.client.post(reverse('orders:checkout', args=[book.pk]), {
                'delivery_address': '1 MG Road', 'delivery_city': 'Kanpur', 'delivery_pincode': '208001',
                'payment_method': 'cod',
            })
        self.assertEqual(Order.objects.count(), 0)
        book.refresh_from_db()
        self.assertEqual(book.status, 'available')

    def test_fraud_check_flags_a_non_positive_price(self):
        flags = check_suspicious_price(Decimal('0'))
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['severity'], 'high')
