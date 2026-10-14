import io
import shutil
import tempfile
import time
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

import unittest

from . import throttle, validators
from .models import User

PW = 'Str0ng!Pass#123'


def _png(size=(40, 30), name='p.png'):
    buf = io.BytesIO()
    Image.new('RGB', size, 'green').save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def _token(age=10):
    with mock.patch('accounts.throttle.time.time', return_value=time.time() - age):
        return throttle.make_form_token()


def _signup(username='alice', email='alice@example.com', **extra):
    data = {
        'username': username, 'email': email, 'role': 'both', 'phone': '', 'city': '',
        'password1': PW, 'password2': PW, 'referral_code': '', 'form_token': _token(),
    }
    data.update(extra)
    return data


class GstinPanValidatorTests(TestCase):
    def test_known_valid_gstins_pass(self):
        self.assertEqual(validators.validate_gstin('27AAPFU0939F1ZV'), '27AAPFU0939F1ZV')
        self.assertEqual(validators.validate_gstin(' 29aagcb7383j1z4 '), '29AAGCB7383J1Z4')  # trimmed + upper-cased

    def test_checksum_failure(self):
        with self.assertRaises(ValidationError) as ctx:
            validators.validate_gstin('27AAPFU0939F1ZX')
        self.assertEqual(ctx.exception.code, 'gstin_checksum')

    def test_wrong_length_format_and_state(self):
        for value, code in [('27AAPFU0939F1Z', 'gstin_length'), ('27AAPFU0939F1YV', 'gstin_format'), ('AAAAAAAAAAAAAAA', 'gstin_format')]:
            with self.assertRaises(ValidationError) as ctx:
                validators.validate_gstin(value)
            self.assertEqual(ctx.exception.code, code, value)
        # State code 00 / 50 do not exist; build one with a correct checksum so only the state is wrong.
        body = '50AAPFU0939F1Z'
        with self.assertRaises(ValidationError) as ctx:
            validators.validate_gstin(body + validators.gstin_check_char(body))
        self.assertEqual(ctx.exception.code, 'gstin_state')

    def test_pan(self):
        self.assertEqual(validators.validate_pan('abcpe1234f'), 'ABCPE1234F')
        for bad in ['ABCDE1234F', 'ABCPE12345', 'ABCP1234F', '']:  # 4th char D is not a holder type
            with self.assertRaises(ValidationError):
                validators.validate_pan(bad)


class ProofImageTests(TestCase):
    def test_png_roundtrips_and_is_reencoded(self):
        out = validators.clean_proof_image(_png(), 'ID proof')
        self.assertTrue(out.name.endswith('.jpg'))
        Image.open(out).verify()

    def test_gps_exif_is_stripped(self):
        img = Image.new('RGB', (50, 50), 'blue')
        exif = Image.Exif()
        exif[0x8825] = {1: 'N'}      # GPSInfo IFD
        exif[0x010F] = 'TestCamera'  # Make
        buf = io.BytesIO()
        img.save(buf, 'JPEG', exif=exif)
        out = validators.clean_proof_image(SimpleUploadedFile('g.jpg', buf.getvalue(), 'image/jpeg'))
        self.assertFalse(dict(Image.open(out).getexif()))

    def test_rejects_non_image_and_renamed_files(self):
        with self.assertRaises(ValidationError):
            validators.clean_proof_image(SimpleUploadedFile('a.jpg', b'%PDF-1.4 not an image', 'image/jpeg'))

    def test_rejects_oversize(self):
        big = SimpleUploadedFile('big.jpg', b'0' * (validators.MAX_PROOF_BYTES + 1), 'image/jpeg')
        with self.assertRaisesMessage(ValidationError, 'limit is 5 MB'):
            validators.clean_proof_image(big, 'shop proof')

    def test_rejects_unsupported_format(self):
        buf = io.BytesIO()
        Image.new('RGB', (20, 20)).save(buf, 'GIF')
        with self.assertRaises(ValidationError):
            validators.clean_proof_image(SimpleUploadedFile('a.gif', buf.getvalue(), 'image/gif'))

    def test_existing_saved_file_is_left_alone(self):
        marker = object()
        self.assertIs(validators.clean_proof_image(marker), marker)

    @unittest.skipUnless(validators.HEIC_SUPPORTED, 'pillow-heif not installed')
    def test_heic_is_converted_to_jpeg(self):
        buf = io.BytesIO()
        Image.new('RGB', (64, 64), 'red').save(buf, 'HEIF')
        out = validators.clean_proof_image(SimpleUploadedFile('IMG_0001.HEIC', buf.getvalue(), 'image/heic'))
        self.assertTrue(out.name.endswith('.jpg'))
        self.assertEqual(Image.open(out).format, 'JPEG')


class AvailabilityEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('accounts:check_availability')
        User.objects.create_user('taken', 'taken@example.com', PW)

    def get(self, field, value):
        return self.client.get(self.url, {'field': field, 'value': value})

    def test_username_taken_case_insensitive(self):
        self.assertFalse(self.get('username', 'TAKEN').json()['available'])
        self.assertTrue(self.get('username', 'fresh_name').json()['available'])

    def test_username_with_illegal_characters(self):
        data = self.get('username', 'bad name!').json()
        self.assertFalse(data['available'])

    def test_email_taken_but_unverified_signup_does_not_block(self):
        self.assertFalse(self.get('email', 'TAKEN@example.com').json()['available'])
        User.objects.create_user('ghost', 'ghost@example.com', PW, signup_pending=True, is_active=False)
        self.assertTrue(self.get('email', 'ghost@example.com').json()['available'])

    def test_invalid_email_and_empty_values(self):
        self.assertFalse(self.get('email', 'not-an-email').json()['available'])
        self.assertIsNone(self.get('email', '').json()['available'])

    def test_unknown_field_is_400(self):
        self.assertEqual(self.get('phone', '123').status_code, 400)

    @override_settings(AVAILABILITY_CHECKS_PER_IP_PER_MINUTE=3)
    def test_rate_limited(self):
        for _ in range(3):
            self.assertEqual(self.get('username', 'abc').status_code, 200)
        r = self.get('username', 'abc')
        self.assertEqual(r.status_code, 429)
        self.assertIn('Retry-After', r)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SignupProtectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('accounts:signup')

    def post(self, **kw):
        return self.client.post(self.url, _signup(**kw))

    def test_normal_signup_still_works(self):
        r = self.post()
        self.assertRedirects(r, reverse('accounts:verify_signup'))
        self.assertTrue(User.objects.filter(username='alice', signup_pending=True).exists())

    def test_honeypot_blocks_without_creating_account(self):
        r = self.post(website='http://spam.example')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(User.objects.filter(username='alice').exists())

    def test_missing_forged_or_too_fast_token_blocks(self):
        for token in ['', 'garbage:token', _token(age=0)]:
            r = self.post(form_token=token)
            self.assertEqual(r.status_code, 400, token)
        self.assertFalse(User.objects.filter(username='alice').exists())

    def test_expired_token_blocks(self):
        self.assertEqual(self.post(form_token=_token(age=throttle.MAX_TOKEN_AGE_SECONDS + 60)).status_code, 400)

    @override_settings(SIGNUP_MAX_ACCOUNTS_PER_IP_PER_HOUR=2)
    def test_account_cap_per_ip(self):
        self.assertEqual(self.post(username='a1', email='a1@example.com').status_code, 302)
        self.assertEqual(self.post(username='a2', email='a2@example.com').status_code, 302)
        r = self.post(username='a3', email='a3@example.com')
        self.assertEqual(r.status_code, 429)
        self.assertFalse(User.objects.filter(username='a3').exists())

    @override_settings(SIGNUP_MAX_ACCOUNTS_PER_IP_PER_HOUR=2)
    def test_validation_errors_do_not_use_up_account_budget(self):
        for _ in range(5):
            self.assertEqual(self.post(username='a1', email='not-an-email').status_code, 200)
        self.assertEqual(self.post(username='a1', email='a1@example.com').status_code, 302)

    @override_settings(SIGNUP_MAX_ATTEMPTS_PER_IP_PER_HOUR=3)
    def test_attempt_cap_per_ip(self):
        for _ in range(3):
            self.post(email='bad')
        self.assertEqual(self.post(email='bad').status_code, 429)

    @override_settings(SIGNUP_MAX_PER_EMAIL_PER_DAY=1)
    def test_same_address_is_only_emailed_once_per_day(self):
        self.post(username='a1', email='same@example.com')
        self.assertEqual(len(mail.outbox), 1)
        self.client.session.flush()
        User.objects.filter(username='a1').delete()   # e.g. they cancelled and tried again
        self.post(username='a2', email='same@example.com')
        self.assertEqual(len(mail.outbox), 1)  # no second code mailed to the same inbox

    @override_settings(TURNSTILE_SITE_KEY='site', TURNSTILE_SECRET_KEY='secret')
    def test_captcha_required_when_configured(self):
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertFalse(User.objects.filter(username='alice').exists())

    @override_settings(TURNSTILE_SITE_KEY='site', TURNSTILE_SECRET_KEY='secret')
    def test_captcha_accepted_and_rejected(self):
        resp = mock.Mock(); resp.json.return_value = {'success': True}
        with mock.patch('accounts.throttle.requests.post', return_value=resp):
            r = self.client.post(self.url, {**_signup(), 'cf-turnstile-response': 'tok'})
        self.assertEqual(r.status_code, 302)
        resp.json.return_value = {'success': False}
        with mock.patch('accounts.throttle.requests.post', return_value=resp):
            r = self.client.post(self.url, {**_signup('bob', 'bob@example.com'), 'cf-turnstile-response': 'tok'})
        self.assertEqual(r.status_code, 400)

    @override_settings(TURNSTILE_SITE_KEY='site', TURNSTILE_SECRET_KEY='secret')
    def test_captcha_fails_closed_when_cloudflare_unreachable(self):
        import requests
        with mock.patch('accounts.throttle.requests.post', side_effect=requests.ConnectionError):
            r = self.client.post(self.url, {**_signup(), 'cf-turnstile-response': 'tok'})
        self.assertEqual(r.status_code, 400)

    def test_signup_page_renders_helpers(self):
        html = self.client.get(self.url).content.decode()
        for needle in ['auth-ui.js', 'upload-preview.js', 'data-availability-url', 'name="form_token"', 'name="website"', 'data-password-toggle']:
            self.assertIn(needle, html)

    @override_settings(NUM_PROXIES=1)
    def test_forwarded_ip_only_trusted_with_configured_proxies(self):
        req = mock.Mock(META={'REMOTE_ADDR': '10.0.0.1', 'HTTP_X_FORWARDED_FOR': '1.2.3.4, 203.0.113.9'})
        self.assertEqual(throttle.get_client_ip(req), '203.0.113.9')   # last hop = what OUR proxy saw
        with override_settings(NUM_PROXIES=0):
            self.assertEqual(throttle.get_client_ip(req), '10.0.0.1')
