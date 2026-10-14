from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from books.models import Book, Category
from wallet.models import WalletTransaction
from wallet.services import get_wallet
from .models import Order, Payment, Shipment, MAX_DELIVERY_OTP_ATTEMPTS
from .views import _mark_book_taken


class DeliveryOtpLockoutTests(TestCase):
    """confirm_delivery locks out further guesses on a shipment's
    delivery_otp after MAX_DELIVERY_OTP_ATTEMPTS wrong tries, since this
    OTP is what releases escrowed payment - see Shipment.check_delivery_otp
    and orders.views.confirm_delivery."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.buyer = User.objects.create_user(username='buyer1', password='pw')
        category, _ = Category.objects.get_or_create(name='Test Category')
        self.book = Book.objects.create(
            seller=self.seller, category=category, title='Some Book', price=199, status='sold',
        )
        self.order = Order.objects.create(buyer=self.buyer, book=self.book, final_price=199)
        self.shipment = Shipment.objects.create(order=self.order, delivery_otp='123456')
        self.client.login(username='buyer1', password='pw')
        self.url = reverse('orders:confirm_delivery', args=[self.order.order_id])

    def test_correct_otp_marks_delivered(self):
        self.client.post(self.url, {'otp': '123456'})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'delivered')

    def test_wrong_otp_increments_attempts(self):
        self.client.post(self.url, {'otp': '000000'})
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.delivery_otp_attempts, 1)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, 'delivered')

    def test_locked_out_after_max_attempts_even_with_correct_otp(self):
        for _ in range(MAX_DELIVERY_OTP_ATTEMPTS):
            self.client.post(self.url, {'otp': '000000'})
        self.client.post(self.url, {'otp': '123456'})
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, 'delivered')


class InvoiceDownloadTests(TestCase):
    """download_invoice is only reachable by the order's buyer/seller, and
    only once there's a confirmed payment (not a Razorpay order that was
    created but never actually paid) - see orders.views.download_invoice."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.buyer = User.objects.create_user(username='buyer1', password='pw')
        self.other = User.objects.create_user(username='rando', password='pw')
        category, _ = Category.objects.get_or_create(name='Test Category')
        self.book = Book.objects.create(
            seller=self.seller, category=category, title='Some Book', price=199, status='sold',
        )
        self.order = Order.objects.create(buyer=self.buyer, book=self.book, final_price=199)
        self.url = reverse('orders:download_invoice', args=[self.order.order_id])

    def test_no_payment_yet_redirects_with_error(self):
        self.client.login(username='buyer1', password='pw')
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('orders:detail', args=[self.order.order_id]))

    def test_unpaid_razorpay_order_not_downloadable(self):
        from .models import Payment
        Payment.objects.create(order=self.order, amount=239, method='card', status='created')
        self.client.login(username='buyer1', password='pw')
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('orders:detail', args=[self.order.order_id]))

    def test_confirmed_payment_returns_pdf(self):
        from .models import Payment
        Payment.objects.create(order=self.order, amount=239, method='cod', status='pending')
        self.client.login(username='buyer1', password='pw')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_seller_can_also_download(self):
        from .models import Payment
        Payment.objects.create(order=self.order, amount=239, method='cod', status='pending')
        self.client.login(username='seller1', password='pw')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_unrelated_user_denied(self):
        from .models import Payment
        Payment.objects.create(order=self.order, amount=239, method='cod', status='pending')
        self.client.login(username='rando', password='pw')
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('books:home'))


class InvoiceEmailTests(TestCase):
    """Placing an order auto-emails the PDF invoice to both buyer and
    seller (bookmarket.notify._email_invoice, called from
    notify_order_placed) on top of the existing manual 'Download Invoice'
    button - see orders.views checkout()'s cod/bb_wallet branches."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller1', password='pw', email='seller@example.com')
        self.buyer = User.objects.create_user(
            username='buyer1', password='pw', email='buyer@example.com',
            phone='9876543210', address='1 MG Road', city='Kanpur', pincode='208001',
        )
        category, _ = Category.objects.get_or_create(name='Test Category')
        self.book = Book.objects.create(
            seller=self.seller, category=category, title='Some Book', price=199, status='available',
        )
        self.client.login(username='buyer1', password='pw')

    def test_cod_order_emails_invoice_pdf_to_buyer_and_seller(self):
        from django.core import mail
        response = self.client.post(reverse('orders:checkout', args=[self.book.pk]), {
            'delivery_address': '1 MG Road', 'delivery_city': 'Kanpur', 'delivery_pincode': '208001',
            'payment_method': 'cod',
        })
        self.assertEqual(response.status_code, 302)

        invoice_emails = [m for m in mail.outbox if m.attachments]
        self.assertEqual(len(invoice_emails), 2)
        recipients = {m.to[0] for m in invoice_emails}
        self.assertEqual(recipients, {'buyer@example.com', 'seller@example.com'})
        for m in invoice_emails:
            filename, content, mimetype = m.attachments[0]
            self.assertTrue(filename.startswith('invoice-'))
            self.assertEqual(mimetype, 'application/pdf')
            self.assertTrue(content.startswith(b'%PDF'))

    def test_invoice_email_skipped_when_email_disabled(self):
        from django.core import mail
        from notifications.models import NotificationPreference
        NotificationPreference.objects.create(user=self.buyer, email_enabled=False)

        self.client.post(reverse('orders:checkout', args=[self.book.pk]), {
            'delivery_address': '1 MG Road', 'delivery_city': 'Kanpur', 'delivery_pincode': '208001',
            'payment_method': 'cod',
        })

        invoice_emails = [m for m in mail.outbox if m.attachments]
        recipients = {m.to[0] for m in invoice_emails}
        self.assertNotIn('buyer@example.com', recipients)
        self.assertIn('seller@example.com', recipients)


class DeliveryConfirmationIsOnceOnlyTests(TestCase):
    """confirm_delivery releases escrow and pays wallet cashback. The OTP
    stays valid after a successful confirmation, so without an
    already-delivered guard a buyer could replay the same POST and mint
    cashback on every submit. See orders.views._release_on_delivery."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller_once', password='pw')
        self.buyer = User.objects.create_user(username='buyer_once', password='pw')
        category, _ = Category.objects.get_or_create(name='Test Category')
        self.book = Book.objects.create(
            seller=self.seller, category=category, title='Some Book', price=1000, status='sold',
        )
        self.order = Order.objects.create(
            buyer=self.buyer, book=self.book, final_price=1000, delivery_charge=40,
            payment_method='upi', status='out_for_delivery',
            delivery_address='1 MG Road', delivery_city='Kanpur', delivery_pincode='208001',
        )
        Payment.objects.create(
            order=self.order, amount=self.order.total_amount, method='upi', status='escrow_held',
        )
        Shipment.objects.create(order=self.order, delivery_otp='1234')
        self.client.login(username='buyer_once', password='pw')
        self.url = reverse('orders:confirm_delivery', args=[self.order.order_id])

    def test_cashback_is_credited_exactly_once(self):
        for _ in range(3):
            self.client.post(self.url, {'otp': '1234'})

        # 2% of 1000, once - not once per submit.
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('20.00'))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet__user=self.buyer, source='cashback').count(), 1
        )

    def test_replay_leaves_order_and_payment_settled(self):
        self.client.post(self.url, {'otp': '1234'})
        self.client.post(self.url, {'otp': '1234'})

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'delivered')
        self.assertEqual(self.order.payment.status, 'released')

    def test_get_is_rejected(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'out_for_delivery')
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('0'))


class ListingClaimRaceTests(TestCase):
    """_mark_book_taken is the real availability gate - it re-checks and
    flips the listing status under a row lock, so two buyers checking out
    at the same moment can't both end up with an order for one copy."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller_race', password='pw')
        self.buyer_a = User.objects.create_user(username='buyer_a', password='pw')
        self.buyer_b = User.objects.create_user(username='buyer_b', password='pw')
        category, _ = Category.objects.get_or_create(name='Test Category')
        self.book = Book.objects.create(
            seller=self.seller, category=category, title='Some Book', price=500, status='available',
        )

    def _checkout_as(self, user):
        self.client.force_login(user)
        return self.client.post(reverse('orders:checkout', args=[self.book.pk]), {
            'delivery_address': '1 MG Road', 'delivery_city': 'Kanpur', 'delivery_pincode': '208001',
            'payment_method': 'cod',
        })

    def test_second_buyer_cannot_order_a_taken_listing(self):
        self._checkout_as(self.buyer_a)
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'sold')

        # Force the state the race produces: the listing is gone, but the
        # second buyer's request got past the early availability check.
        self._checkout_as(self.buyer_b)

        self.assertEqual(Order.objects.filter(book=self.book).count(), 1)
        self.assertEqual(Order.objects.get(book=self.book).buyer, self.buyer_a)

    def test_losing_the_claim_leaves_no_order_behind(self):
        order = Order(
            buyer=self.buyer_b, book=self.book, final_price=500, delivery_charge=40,
            payment_method='cod', delivery_address='x', delivery_city='y', delivery_pincode='208001',
        )
        self.book.status = 'sold'
        self.book.save(update_fields=['status'])

        self.assertFalse(_mark_book_taken(self.book, order))
