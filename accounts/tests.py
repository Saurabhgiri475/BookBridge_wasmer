from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from books.models import Book, Category
from .models import User, MAX_OTP_ATTEMPTS


def make_book(seller, **kwargs):
    category, _ = Category.objects.get_or_create(name='Test Category')
    defaults = {'title': 'Some Book', 'price': 199, 'status': 'available'}
    defaults.update(kwargs)
    return Book.objects.create(seller=seller, category=category, **defaults)


class ListingPerformanceTests(TestCase):
    """accounts:dashboard's Listing Performance table - turns Book.views/
    offers/wishlist counts into something a seller can act on. See
    accounts/views.py:dashboard and templates/accounts/dashboard.html."""

    def setUp(self):
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.client.login(username='seller1', password='pw')

    def test_only_available_listings_included(self):
        make_book(self.seller, title='Available Book', status='available')
        make_book(self.seller, title='Sold Book', status='sold')
        response = self.client.get(reverse('accounts:dashboard'))
        titles = [b.title for b in response.context['listing_performance']]
        self.assertIn('Available Book', titles)
        self.assertNotIn('Sold Book', titles)

    def test_views_and_offer_count_annotated(self):
        from orders.models import Offer
        buyer = User.objects.create_user(username='buyer1', password='pw')
        book = make_book(self.seller, title='Popular Book', status='available', views=25)
        Offer.objects.create(book=book, buyer=buyer, amount=100)
        response = self.client.get(reverse('accounts:dashboard'))
        row = response.context['listing_performance'][0]
        self.assertEqual(row.views, 25)
        self.assertEqual(row.offer_count, 1)

    def test_nudge_shown_for_high_views_no_offers(self):
        make_book(self.seller, title='Stale Book', status='available', views=25)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertContains(response, 'try lowering the price')

    def test_no_nudge_when_offers_exist(self):
        from orders.models import Offer
        buyer = User.objects.create_user(username='buyer1', password='pw')
        book = make_book(self.seller, title='Active Book', status='available', views=25)
        Offer.objects.create(book=book, buyer=buyer, amount=100)
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertNotContains(response, 'try lowering the price')

    def test_other_sellers_listings_not_shown(self):
        other = User.objects.create_user(username='seller2', password='pw')
        make_book(other, title='Not Mine', status='available')
        response = self.client.get(reverse('accounts:dashboard'))
        titles = [b.title for b in response.context['listing_performance']]
        self.assertNotIn('Not Mine', titles)


class OtpLockoutTests(TestCase):
    """check_email_otp locks out further guesses after
    MAX_OTP_ATTEMPTS wrong tries, so a 6-digit OTP can't be brute-forced
    by unlimited submission. See User._verify_otp."""

    def setUp(self):
        self.user = User.objects.create_user(username='buyer1', password='pw')
        self.user.set_otp('email_otp', '123456')
        self.user.email_otp_sent_at = timezone.now()
        self.user.save(update_fields=['email_otp', 'email_otp_sent_at'])

    def test_correct_otp_succeeds(self):
        self.assertTrue(self.user.check_email_otp('123456'))

    def test_wrong_otp_fails_and_counts_as_attempt(self):
        self.assertFalse(self.user.check_email_otp('000000'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email_otp_attempts, 1)

    def test_locked_out_after_max_attempts(self):
        for _ in range(MAX_OTP_ATTEMPTS):
            self.assertFalse(self.user.check_email_otp('000000'))
        # Even the correct OTP is now rejected until a fresh one is sent.
        self.assertFalse(self.user.check_email_otp('123456'))

    def test_successful_check_resets_attempt_counter(self):
        self.user.check_email_otp('000000')
        self.user.check_email_otp('000000')
        self.assertTrue(self.user.check_email_otp('123456'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email_otp_attempts, 0)


class EmailOtpResendCooldownTests(TestCase):
    """send_email_otp used to have no limit on how often a logged-in user
    could request a fresh code - each call also reset email_otp_attempts to
    0, which doubled as a way to undo the brute-force lockout below. See
    User.can_resend_otp."""

    def setUp(self):
        self.user = User.objects.create_user(username='resend_user', password='pw', email='u@example.com')
        self.client.login(username='resend_user', password='pw')
        self.url = reverse('accounts:send_email_otp')

    def test_second_request_within_cooldown_is_refused(self):
        self.client.post(self.url)
        first_otp = User.objects.get(pk=self.user.pk).email_otp

        self.client.post(self.url)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email_otp, first_otp)

    def test_request_after_cooldown_sends_a_new_code(self):
        self.client.post(self.url)
        self.user.refresh_from_db()
        self.user.email_otp_sent_at = timezone.now() - timezone.timedelta(minutes=5)
        self.user.save(update_fields=['email_otp_sent_at'])
        first_otp = self.user.email_otp

        self.client.post(self.url)

        self.user.refresh_from_db()
        self.assertNotEqual(self.user.email_otp, first_otp)

    def test_resend_does_not_reset_a_lockout_within_the_cooldown(self):
        """Resetting attempts on every resend meant a locked-out guesser
        could immediately get 5 fresh tries by resending - the cooldown
        closes that too."""
        self.client.post(self.url)
        self.user.refresh_from_db()
        self.user.email_otp_attempts = MAX_OTP_ATTEMPTS
        self.user.save(update_fields=['email_otp_attempts'])

        self.client.post(self.url)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email_otp_attempts, MAX_OTP_ATTEMPTS)

    def test_get_is_rejected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
