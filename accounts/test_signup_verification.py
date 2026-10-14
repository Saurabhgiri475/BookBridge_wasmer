import io
import re
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from partners.models import ShopkeeperPartner
from wallet.models import WalletTransaction

from .models import MAX_OTP_ATTEMPTS, User
from .otp import otp_matches

PW = 'Str0ng!Pass#123'


def _img(name='p.png'):
    buf = io.BytesIO()
    Image.new('RGB', (30, 30), 'green').save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def _form_token(age_seconds=10):
    """A signup form token that looks like the form was rendered `age_seconds` ago."""
    from unittest import mock
    from . import throttle
    with mock.patch('accounts.throttle.time.time', return_value=__import__('time').time() - age_seconds):
        return throttle.make_form_token()


def signup_data(username='newuser', email='new@example.com', **extra):
    data = {
        'form_token': _form_token(),
        'username': username, 'email': email, 'role': 'both', 'phone': '', 'city': '',
        'password1': PW, 'password2': PW, 'referral_code': '',
    }
    data.update(extra)
    return data


def partner_data(**over):
    data = {
        'apply_as_partner': 'on', 'shop_name': 'Verified Shop', 'shop_address': '1 Road',
        'shop_city': 'Lucknow', 'shop_pincode': '226001', 'contact_phone': '9999999999',
        'gstin': '', 'owner_aadhaar_number': '234567890123',
        'owner_id_proof': _img('id.png'), 'shop_proof': _img('shop.png'),
    }
    data.update(over)
    return data


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SignupEmailVerificationTests(TestCase):
    def setUp(self):
        cache.clear()  # rate-limit counters live in the cache

    def signup(self, **kw):
        return self.client.post(reverse('accounts:signup'), signup_data(**kw))

    def code(self, username='newuser'):
        """The code the user actually received: the database only keeps a hash of it, so read it from the email."""
        email = User.objects.get(username=username).email
        for message in reversed(mail.outbox):
            found = re.search(r'verification code is (\d{6})', message.body)
            if found and email in message.to:
                return found.group(1)
        raise AssertionError('no verification email was sent to ' + email)

    def verify(self, code):
        return self.client.post(reverse('accounts:verify_signup'), {'action': 'verify', 'otp': code})

    # --- signup step ---------------------------------------------------
    def test_signup_creates_inactive_pending_account_and_emails_code(self):
        r = self.signup()
        self.assertRedirects(r, reverse('accounts:verify_signup'))
        u = User.objects.get(username='newuser')
        self.assertFalse(u.is_active)
        self.assertTrue(u.signup_pending)
        self.assertFalse(u.email_verified)
        self.assertNotIn('_auth_user_id', self.client.session)  # not logged in
        self.assertEqual(len(mail.outbox), 1)
        self.assertRegex(mail.outbox[0].body, r'verification code is \d{6}')
        self.assertNotIn(self.code(), u.email_otp)  # only a hash is stored, never the code
        self.assertTrue(otp_matches(u.email_otp, 'email_otp', u.pk, self.code()))
        self.assertEqual(mail.outbox[0].to, ['new@example.com'])

    def test_welcome_email_and_referral_bonus_wait_for_verification(self):
        referrer = User.objects.create_user('ref', 'ref@example.com', PW)
        self.signup(referral_code=referrer.referral_code)
        self.assertEqual(len(mail.outbox), 1)  # only the code, no welcome yet
        self.assertFalse(WalletTransaction.objects.filter(source='referral').exists())
        self.verify(self.code())
        self.assertTrue(WalletTransaction.objects.filter(wallet__user__username='newuser', source='referral').exists())
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('Welcome', mail.outbox[1].subject)

    # --- verification step ----------------------------------------------
    def test_correct_code_activates_logs_in_and_marks_email_verified(self):
        self.signup()
        r = self.verify(self.code())
        self.assertRedirects(r, reverse('books:home'))
        u = User.objects.get(username='newuser')
        self.assertTrue(u.is_active)
        self.assertTrue(u.email_verified)
        self.assertFalse(u.signup_pending)
        self.assertEqual(u.email_otp, '')
        self.assertEqual(int(self.client.session['_auth_user_id']), u.pk)

    def test_wrong_code_keeps_account_inactive(self):
        self.signup()
        real = self.code()
        wrong = '000000' if real != '000000' else '111111'
        r = self.verify(wrong)
        self.assertRedirects(r, reverse('accounts:verify_signup'))
        u = User.objects.get(username='newuser')
        self.assertFalse(u.is_active)
        self.assertEqual(u.email_otp_attempts, 1)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_lockout_after_max_wrong_attempts_even_for_the_right_code(self):
        self.signup()
        real = self.code()
        wrong = '000000' if real != '000000' else '111111'
        for _ in range(MAX_OTP_ATTEMPTS):
            self.verify(wrong)
        self.verify(real)
        self.assertFalse(User.objects.get(username='newuser').is_active)

    def test_expired_code_rejected(self):
        self.signup()
        User.objects.filter(username='newuser').update(email_otp_sent_at=timezone.now() - timedelta(minutes=11))
        self.verify(self.code())
        self.assertFalse(User.objects.get(username='newuser').is_active)

    def test_non_numeric_or_short_code_does_not_burn_attempts(self):
        self.signup()
        self.verify('abc')
        self.assertEqual(User.objects.get(username='newuser').email_otp_attempts, 0)

    # --- resend ---------------------------------------------------------
    def test_resend_blocked_during_cooldown(self):
        self.signup()
        first = self.code()
        self.client.post(reverse('accounts:verify_signup'), {'action': 'resend'})
        self.assertEqual(self.code(), first)
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_after_cooldown_sends_new_code_and_resets_attempts(self):
        self.signup()
        self.verify('000000')
        User.objects.filter(username='newuser').update(email_otp_sent_at=timezone.now() - timedelta(minutes=2))
        self.client.post(reverse('accounts:verify_signup'), {'action': 'resend'})
        u = User.objects.get(username='newuser')
        self.assertEqual(u.email_otp_attempts, 0)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(otp_matches(u.email_otp, 'email_otp', u.pk, self.code()))

    # --- partner signups ------------------------------------------------
    def test_partner_signup_saves_application_then_redirects_to_status_after_verification(self):
        r = self.client.post(reverse('accounts:signup'), {**signup_data('shopper', 'shop@example.com'), **partner_data()})
        self.assertRedirects(r, reverse('accounts:verify_signup'))
        self.assertTrue(ShopkeeperPartner.objects.filter(user__username='shopper', status='pending').exists())
        r = self.client.post(reverse('accounts:verify_signup'), {'action': 'verify', 'otp': self.code('shopper')})
        self.assertRedirects(r, reverse('partners:apply'))
        page = self.client.get(reverse('partners:apply'))
        self.assertContains(page, 'Your application form has been submitted!')

    # --- login interaction ----------------------------------------------
    def test_login_with_pending_account_sends_user_back_to_verification(self):
        self.signup()
        self.client.logout()
        fresh = self.client_class()
        r = fresh.post(reverse('accounts:login'), {'username': 'newuser', 'password': PW})
        self.assertRedirects(r, reverse('accounts:verify_signup'))
        r = fresh.post(reverse('accounts:verify_signup'), {'action': 'verify', 'otp': self.code()})
        self.assertRedirects(r, reverse('books:home'))

    def test_login_with_wrong_password_shows_visible_error(self):
        self.signup()
        fresh = self.client_class()
        r = fresh.post(reverse('accounts:login'), {'username': 'newuser', 'password': 'nope-nope'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'alert-danger')
        self.assertContains(r, 'correct username and password')

    def test_admin_deactivated_user_is_not_reactivated_and_not_sent_to_verify(self):
        User.objects.create_user('banned', 'b@example.com', PW, is_active=False)
        fresh = self.client_class()
        r = fresh.post(reverse('accounts:login'), {'username': 'banned', 'password': PW})
        self.assertEqual(r.status_code, 200)  # generic login failure, no verify redirect
        # a session pointing at that user must not let a code switch the account on
        session = fresh.session
        session['pending_signup_user_id'] = User.objects.get(username='banned').pk
        session.save()
        r = fresh.get(reverse('accounts:verify_signup'))
        self.assertRedirects(r, reverse('accounts:login'))

    def test_verify_page_without_pending_session_redirects_to_login(self):
        self.assertRedirects(self.client.get(reverse('accounts:verify_signup')), reverse('accounts:login'))

    def test_verify_page_shows_masked_email(self):
        self.signup(email='sensitive.person@example.com')
        r = self.client.get(reverse('accounts:verify_signup'))
        self.assertContains(r, 's****')
        self.assertNotContains(r, 'sensitive.person')

    # --- start over ------------------------------------------------------
    def test_cancel_deletes_pending_account_and_frees_username(self):
        self.signup()
        r = self.client.post(reverse('accounts:verify_signup'), {'action': 'cancel'})
        self.assertRedirects(r, reverse('accounts:signup'))
        self.assertFalse(User.objects.filter(username='newuser').exists())
        self.assertRedirects(self.signup(), reverse('accounts:verify_signup'))

    # --- unique email ------------------------------------------------------
    def test_duplicate_email_rejected_case_insensitively(self):
        User.objects.create_user('taken', 'Person@Example.com', PW)
        r = self.signup(username='second', email='person@example.com')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username='second').exists())
        self.assertContains(r, 'already exists')

    def test_unverified_signup_does_not_block_the_real_owner_of_the_email(self):
        self.signup(username='squatter', email='victim@example.com')
        r = self.client_class().post(reverse('accounts:signup'), signup_data('victim', 'victim@example.com'))
        self.assertRedirects(r, reverse('accounts:verify_signup'))

    def test_verification_blocked_if_email_got_taken_by_a_verified_account_meanwhile(self):
        self.signup(username='slow', email='race@example.com')
        User.objects.create_user('fast', 'race@example.com', PW)
        self.verify(self.code('slow'))
        self.assertFalse(User.objects.get(username='slow').is_active)


class PurgeUnverifiedSignupsTests(TestCase):
    def test_deletes_only_stale_pending_accounts(self):
        old = User.objects.create_user('old', 'o@example.com', PW, is_active=False, signup_pending=True)
        User.objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(hours=72))
        User.objects.create_user('fresh', 'f@example.com', PW, is_active=False, signup_pending=True)
        User.objects.create_user('real', 'r@example.com', PW)
        banned = User.objects.create_user('banned', 'b@example.com', PW, is_active=False)
        User.objects.filter(pk=banned.pk).update(date_joined=timezone.now() - timedelta(hours=72))
        call_command('purge_unverified_signups', stdout=io.StringIO())
        self.assertEqual(set(User.objects.values_list('username', flat=True)), {'fresh', 'real', 'banned'})

    def test_dry_run_deletes_nothing(self):
        old = User.objects.create_user('old', 'o@example.com', PW, is_active=False, signup_pending=True)
        User.objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(hours=72))
        call_command('purge_unverified_signups', '--dry-run', stdout=io.StringIO())
        self.assertTrue(User.objects.filter(username='old').exists())
