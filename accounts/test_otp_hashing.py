import re

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import User
from .otp import hash_otp, otp_matches

PW = 'Str0ng!Pass#123'


class OtpHashUnitTests(TestCase):
    def test_hash_is_keyed_deterministic_and_not_the_code(self):
        h = hash_otp('email_otp', 7, '123456')
        self.assertEqual(h, hash_otp('email_otp', 7, ' 123456 '))  # surrounding spaces are ignored
        self.assertEqual(len(h), 64)
        self.assertNotIn('123456', h)

    def test_hash_is_bound_to_user_and_purpose(self):
        h = hash_otp('email_otp', 7, '123456')
        self.assertNotEqual(h, hash_otp('email_otp', 8, '123456'))      # another user
        self.assertNotEqual(h, hash_otp('other_otp', 7, '123456'))    # another field
        self.assertFalse(otp_matches(h, 'other_otp', 7, '123456'))

    def test_depends_on_the_secret_key(self):
        with override_settings(SECRET_KEY='another-secret-key-for-this-test-only-0123456789'):
            other = hash_otp('email_otp', 7, '123456')
        self.assertNotEqual(other, hash_otp('email_otp', 7, '123456'))

    def test_empty_values_never_match(self):
        self.assertFalse(otp_matches('', 'email_otp', 1, '123456'))
        self.assertFalse(otp_matches(hash_otp('email_otp', 1, ''), 'email_otp', 1, ''))
        self.assertFalse(otp_matches(hash_otp('email_otp', 1, '123456'), 'email_otp', 1, '12345'))


class OtpModelTests(TestCase):
    def test_set_otp_requires_a_saved_user(self):
        with self.assertRaises(ValueError):
            User(username='nobody').set_otp('email_otp', '123456')

    def test_hash_copied_from_another_account_does_not_verify(self):
        a = User.objects.create_user('a', 'a@x.com', PW)
        b = User.objects.create_user('b', 'b@x.com', PW)
        a.set_otp('email_otp', '123456')
        a.email_otp_sent_at = timezone.now()
        a.save()
        b.email_otp, b.email_otp_sent_at = a.email_otp, timezone.now()  # attacker with DB write access pastes A's hash
        b.save()
        self.assertTrue(a.check_email_otp('123456'))
        self.assertFalse(b.check_email_otp('123456'))

    def test_legacy_plaintext_value_no_longer_verifies(self):
        u = User.objects.create_user('legacy', 'l@x.com', PW)
        u.email_otp, u.email_otp_sent_at = '123456', timezone.now()
        u.save()
        self.assertFalse(u.check_email_otp('123456'))


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class OtpFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('seller', 'seller@x.com', PW, phone='9999900000')
        self.client.login(username='seller', password=PW)

    def test_email_code_is_emailed_but_only_a_hash_is_stored_and_it_verifies(self):
        self.client.post(reverse('accounts:send_email_otp'))
        code = re.search(r'verification code is (\d{6})', mail.outbox[-1].body).group(1)
        self.user.refresh_from_db()
        self.assertEqual(len(self.user.email_otp), 64)
        self.assertNotEqual(self.user.email_otp, code)
        self.client.post(reverse('accounts:confirm_email_otp'), {'otp': code})
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.user.email_otp, '')

    def test_wrong_code_does_not_verify(self):
        self.client.post(reverse('accounts:send_email_otp'))
        self.client.post(reverse('accounts:confirm_email_otp'), {'otp': '000000'})
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
