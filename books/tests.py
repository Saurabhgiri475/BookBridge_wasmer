from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from notifications.models import Notification
from .models import Book, Category, ListingReport, Wishlist


def make_book(seller, **kwargs):
    category, _ = Category.objects.get_or_create(name='Test Category')
    defaults = {'title': 'Some Book', 'price': 199, 'status': 'available'}
    defaults.update(kwargs)
    return Book.objects.create(seller=seller, category=category, **defaults)


class ReportBookTests(TestCase):
    """books:report - the buyer-facing counterpart to the automated Fraud
    Detection scan. See books/views.py:report_book and
    adminpanel/views.py's Reported Listings card for how staff act on
    these afterwards."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.buyer = User.objects.create_user(username='buyer1', password='pw')
        self.book = make_book(self.seller, title='Suspicious Book')

    def test_login_required(self):
        response = self.client.get(reverse('books:report', args=[self.book.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_get_shows_form(self):
        self.client.login(username='buyer1', password='pw')
        response = self.client.get(reverse('books:report', args=[self.book.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Suspicious Book')
        self.assertContains(response, 'Report')

    def test_seller_cannot_report_own_listing(self):
        self.client.login(username='seller1', password='pw')
        response = self.client.post(
            reverse('books:report', args=[self.book.pk]),
            {'reason': 'fake', 'details': 'testing'},
        )
        self.assertRedirects(response, reverse('books:detail', args=[self.book.pk]))
        self.assertFalse(ListingReport.objects.exists())

    def test_valid_submission_creates_report(self):
        self.client.login(username='buyer1', password='pw')
        response = self.client.post(
            reverse('books:report', args=[self.book.pk]),
            {'reason': 'fake', 'details': 'Looks like a stock photo, not the actual book.'},
        )
        self.assertRedirects(response, reverse('books:detail', args=[self.book.pk]))
        report = ListingReport.objects.get()
        self.assertEqual(report.book, self.book)
        self.assertEqual(report.reporter, self.buyer)
        self.assertEqual(report.reason, 'fake')
        self.assertEqual(report.status, 'pending')

    def test_duplicate_report_from_same_user_is_blocked(self):
        self.client.login(username='buyer1', password='pw')
        self.client.post(reverse('books:report', args=[self.book.pk]), {'reason': 'fake', 'details': ''})
        self.client.post(reverse('books:report', args=[self.book.pk]), {'reason': 'scam', 'details': ''})
        self.assertEqual(ListingReport.objects.filter(book=self.book, reporter=self.buyer).count(), 1)

    def test_two_different_users_can_both_report(self):
        other_buyer = User.objects.create_user(username='buyer2', password='pw')
        self.client.login(username='buyer1', password='pw')
        self.client.post(reverse('books:report', args=[self.book.pk]), {'reason': 'fake', 'details': ''})
        self.client.login(username='buyer2', password='pw')
        self.client.post(reverse('books:report', args=[self.book.pk]), {'reason': 'scam', 'details': ''})
        self.assertEqual(ListingReport.objects.filter(book=self.book).count(), 2)

    def test_invalid_reason_does_not_create_report(self):
        self.client.login(username='buyer1', password='pw')
        self.client.post(reverse('books:report', args=[self.book.pk]), {'reason': 'not-a-real-choice', 'details': ''})
        self.assertFalse(ListingReport.objects.exists())


def edit_post_data(**overrides):
    """Minimal valid POST body for books:edit, plus the formset
    management fields Django requires even when there are zero image
    forms in the request."""
    data = {
        'listing_type': 'physical', 'condition': 'good', 'language': 'English',
        'quantity': 1, 'pickup_city': 'Delhi', 'pickup_pincode': '110001', 'pickup_address': 'x',
        'images-TOTAL_FORMS': '0', 'images-INITIAL_FORMS': '0',
        'images-MIN_NUM_FORMS': '0', 'images-MAX_NUM_FORMS': '6',
    }
    data.update(overrides)
    return data


class PriceDropNotificationTests(TestCase):
    """books:edit - lowering the price on a still-visible listing notifies
    everyone who has it wishlisted. See books/views.py:edit_book and
    bookmarket/notify.py:notify_price_drop."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.buyer = User.objects.create_user(username='buyer1', password='pw', email='buyer1@example.com')
        self.category = Category.objects.get_or_create(name='Test Category')[0]
        self.book = make_book(self.seller, title='Textbook', price=250)
        Wishlist.objects.create(user=self.buyer, book=self.book)
        self.client.login(username='seller1', password='pw')

    def test_lowering_price_notifies_wishlisters(self):
        response = self.client.post(
            reverse('books:edit', args=[self.book.pk]),
            edit_post_data(category=self.category.pk, title='Textbook', price='150'),
        )
        self.assertRedirects(response, reverse('books:detail', args=[self.book.pk]))
        notif = Notification.objects.filter(user=self.buyer).first()
        self.assertIsNotNone(notif)
        self.assertIn('Price drop', notif.title)

    def test_raising_price_does_not_notify(self):
        self.client.post(
            reverse('books:edit', args=[self.book.pk]),
            edit_post_data(category=self.category.pk, title='Textbook', price='300'),
        )
        self.assertFalse(Notification.objects.filter(user=self.buyer).exists())

    def test_only_wishlisters_of_that_book_are_notified(self):
        other_book = make_book(self.seller, title='Other Book', price=100)
        other_buyer = User.objects.create_user(username='buyer2', password='pw')
        Wishlist.objects.create(user=other_buyer, book=other_book)
        self.client.post(
            reverse('books:edit', args=[self.book.pk]),
            edit_post_data(category=self.category.pk, title='Textbook', price='150'),
        )
        self.assertFalse(Notification.objects.filter(user=other_buyer).exists())


class BumpListingTests(TestCase):
    """books:bump - a seller-paid, wallet-debited "feature this listing"
    boost. See books/views.py:bump_listing, FEATURED_BUMP_OPTIONS, and
    Book.is_currently_featured()."""

    def setUp(self):
        from wallet.services import credit
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.book = make_book(self.seller, title='Bumpable', status='available')
        self.client.login(username='seller1', password='pw')
        self.credit = credit

    def test_only_seller_can_bump(self):
        other = User.objects.create_user(username='other', password='pw')
        self.client.logout()
        self.client.login(username='other', password='pw')
        response = self.client.get(reverse('books:bump', args=[self.book.pk]))
        self.assertEqual(response.status_code, 404)

    def test_insufficient_balance_redirects_to_wallet_and_does_not_feature(self):
        response = self.client.post(reverse('books:bump', args=[self.book.pk]), {'days': '3'})
        self.assertRedirects(response, reverse('wallet:detail'))
        self.book.refresh_from_db()
        self.assertIsNone(self.book.featured_until)

    def test_successful_bump_debits_wallet_and_sets_featured_until(self):
        from wallet.models import Wallet, WalletTransaction
        self.credit(self.seller, 100, 'cashback')
        response = self.client.post(reverse('books:bump', args=[self.book.pk]), {'days': '3'})
        self.assertRedirects(response, reverse('books:detail', args=[self.book.pk]))
        self.book.refresh_from_db()
        self.assertIsNotNone(self.book.featured_until)
        self.assertTrue(self.book.is_currently_featured())
        wallet = Wallet.objects.get(user=self.seller)
        self.assertEqual(wallet.balance, 71)  # 100 - 29 for 3 days
        self.assertTrue(WalletTransaction.objects.filter(wallet=wallet, source='featured_listing').exists())

    def test_invalid_days_choice_rejected(self):
        self.credit(self.seller, 100, 'cashback')
        response = self.client.post(reverse('books:bump', args=[self.book.pk]), {'days': '999'})
        self.assertRedirects(response, reverse('books:bump', args=[self.book.pk]))
        self.book.refresh_from_db()
        self.assertIsNone(self.book.featured_until)

    def test_second_bump_stacks_on_remaining_time(self):
        self.credit(self.seller, 200, 'cashback')
        self.client.post(reverse('books:bump', args=[self.book.pk]), {'days': '3'})
        self.book.refresh_from_db()
        first_expiry = self.book.featured_until
        self.client.post(reverse('books:bump', args=[self.book.pk]), {'days': '7'})
        self.book.refresh_from_db()
        self.assertGreater((self.book.featured_until - first_expiry).days, 6)

    def test_cannot_bump_unavailable_listing(self):
        self.credit(self.seller, 100, 'cashback')
        self.book.status = 'sold'
        self.book.save(update_fields=['status'])
        response = self.client.post(reverse('books:bump', args=[self.book.pk]), {'days': '3'})
        self.assertRedirects(response, reverse('books:detail', args=[self.book.pk]))
        self.book.refresh_from_db()
        self.assertIsNone(self.book.featured_until)


class BrowseFilterMalformedInputTests(TestCase):
    """book_list passed min_price/max_price/category straight into an ORM
    lookup - a non-numeric query string value (a stray link, a bot, someone
    editing the URL by hand) raised a 500 instead of just being ignored."""

    def setUp(self):
        seller = User.objects.create_user(username='browse_seller', password='pw')
        make_book(seller, title='In-range Book', price=200)

    def test_non_numeric_min_price_does_not_500(self):
        response = self.client.get(reverse('books:home') + '?min_price=abc')
        self.assertIn(response.status_code, (200, 302))

        response = self.client.get('/browse/?min_price=abc')
        self.assertEqual(response.status_code, 200)

    def test_non_numeric_max_price_does_not_500(self):
        response = self.client.get('/browse/?max_price=xyz')
        self.assertEqual(response.status_code, 200)

    def test_non_numeric_category_does_not_500(self):
        response = self.client.get('/browse/?category=not-a-number')
        self.assertEqual(response.status_code, 200)

    def test_valid_price_range_still_filters(self):
        response = self.client.get('/browse/?min_price=100&max_price=300')
        self.assertContains(response, 'In-range Book')


class AboutPageTests(TestCase):
    def test_renders_with_live_numbers_images_and_policy_from_code(self):
        from django.contrib.staticfiles import finders
        from disputes import services as dispute_policy
        resp = self.client.get(reverse('books:about'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for asset in ('img/about/hero.svg', 'img/about/mission.svg'):
            self.assertIn(asset, html)
            self.assertIsNotNone(finders.find(asset), f'{asset} is missing from static/')
        # alt text on both illustrations, and the FAQ quotes the real dispute policy
        self.assertEqual(html.count('alt="Illustration:'), 2)
        self.assertIn(f'{dispute_policy.DISPUTE_WINDOW_DAYS} days of delivery', html)
        self.assertIn(f'{dispute_policy.SELLER_RESPONSE_DAYS} days to reply', html)
        self.assertIn('tel:+917985486802', html)

    def test_counts_are_real(self):
        seller = User.objects.create_user('seller_a', 'sa@example.com', 'pw')
        category, _ = Category.objects.get_or_create(name='About Test Category')
        Book.objects.create(seller=seller, category=category, title='Shown', price=100)
        Book.objects.create(seller=seller, category=category, title='Hidden', price=100, status='sold')
        resp = self.client.get(reverse('books:about'))
        self.assertEqual(resp.context['books_listed'], 1)
        self.assertEqual(resp.context['total_users'], 1)
        self.assertEqual(resp.context['orders_completed'], 0)
        self.assertContains(resp, 'About Test Category')  # category pill shown only when it has live books

    def test_hero_heading_is_not_left_navy_on_the_dark_banner(self):
        import re
        from django.contrib.staticfiles import finders
        css = open(finders.find('css/style.css')).read()
        self.assertRegex(css, re.compile(r'\.about-hero h1[^{]*\{[^}]*color:\s*#fff', re.S))
