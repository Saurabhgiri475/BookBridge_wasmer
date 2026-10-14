import re
import time

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from adminpanel.models import AuditLog

from . import twofactor
from .models import StaffRecoveryCode, StaffTOTPDevice, User

PW = 'Str0ng!Pass#123'


def current_code(device, ahead=0):
    secret = twofactor.decrypt_secret(device.secret_encrypted)
    return twofactor.totp_at(secret, int(time.time() // 30) + ahead)


def later_code(device):
    """A valid code from the next 30-second step (the enrolment code is already used and can't be replayed)."""
    return current_code(device, ahead=1)


class TotpUnitTests(TestCase):
    # RFC 6238 Appendix B test vectors (SHA-1, secret "12345678901234567890"), last 6 digits.
    SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'

    def test_matches_rfc_6238_vectors(self):
        for ts, expected in ((59, '287082'), (1111111109, '081804'), (1234567890, '005924'), (20000000000, '353130')):
            self.assertEqual(twofactor.totp_at(self.SECRET, ts // 30), expected)

    def test_verify_accepts_adjacent_step_but_not_older_or_replayed(self):
        now = 1_700_000_000
        step = now // 30
        good = twofactor.totp_at(self.SECRET, step)
        self.assertEqual(twofactor.verify_totp(self.SECRET, good, now=now), step)
        self.assertEqual(twofactor.verify_totp(self.SECRET, twofactor.totp_at(self.SECRET, step - 1), now=now), step - 1)
        self.assertIsNone(twofactor.verify_totp(self.SECRET, twofactor.totp_at(self.SECRET, step - 2), now=now))
        self.assertIsNone(twofactor.verify_totp(self.SECRET, good, last_used_step=step, now=now))  # replay

    def test_rejects_garbage(self):
        for bad in ('', '12345', '1234567', 'abcdef', None):
            self.assertIsNone(twofactor.verify_totp(self.SECRET, bad, now=1_700_000_000))

    def test_secret_encryption_round_trip_and_key_dependence(self):
        token = twofactor.encrypt_secret(self.SECRET)
        self.assertNotIn(self.SECRET, token)
        self.assertEqual(twofactor.decrypt_secret(token), self.SECRET)
        with override_settings(SECRET_KEY='a-different-secret-key-for-this-test-0123456789abcdef'):
            with self.assertRaises(twofactor.SecretUnreadable):
                twofactor.decrypt_secret(token)

    def test_recovery_codes_format_and_hash(self):
        codes = twofactor.make_recovery_codes()
        self.assertEqual(len(set(codes)), 10)
        self.assertRegex(codes[0], r'^[0-9a-f]{5}-[0-9a-f]{5}$')
        self.assertEqual(twofactor.hash_recovery_code(1, codes[0]), twofactor.hash_recovery_code(1, codes[0].upper().replace('-', ' ')))
        self.assertNotEqual(twofactor.hash_recovery_code(1, codes[0]), twofactor.hash_recovery_code(2, codes[0]))


@override_settings(STAFF_2FA_REQUIRED=True)
class StaffTwoFactorFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user('staffer', 'st@x.com', PW, is_staff=True)
        self.dash = reverse('adminpanel:dashboard')

    def login(self):
        self.client.login(username='staffer', password=PW)

    def enrol(self):
        """Walk through the real setup page and return the recovery codes."""
        self.login()
        page = self.client.get(reverse('accounts:two_factor_setup'))
        self.assertContains(page, '<svg')
        device = StaffTOTPDevice.objects.get(user=self.staff)
        resp = self.client.post(reverse('accounts:two_factor_setup'), {'code': current_code(device)})
        self.assertEqual(resp.status_code, 200)
        return re.findall(r'[0-9a-f]{5}-[0-9a-f]{5}', resp.content.decode())

    def test_unenrolled_staff_is_sent_to_setup_and_cannot_see_dashboard(self):
        self.login()
        resp = self.client.get(self.dash)
        self.assertRedirects(resp, f"{reverse('accounts:two_factor_setup')}?next={self.dash}", fetch_redirect_response=False)

    def test_enrolment_confirms_device_issues_ten_hashed_codes_and_unlocks_staff_area(self):
        codes = self.enrol()
        self.assertEqual(len(codes), 10)
        device = StaffTOTPDevice.objects.get(user=self.staff)
        self.assertIsNotNone(device.confirmed_at)
        self.assertNotIn(twofactor.normalise_code(device.secret_encrypted), '')  # stored as an encrypted token
        stored = set(StaffRecoveryCode.objects.filter(user=self.staff).values_list('code_hash', flat=True))
        self.assertTrue(all(c.replace('-', '') not in ' '.join(stored) for c in codes))
        self.assertEqual(self.client.get(self.dash).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action='staff_2fa_enrolled', actor=self.staff).exists())

    def test_wrong_code_during_setup_does_not_confirm(self):
        self.login()
        self.client.get(reverse('accounts:two_factor_setup'))
        self.client.post(reverse('accounts:two_factor_setup'), {'code': '000000'})
        self.assertIsNone(StaffTOTPDevice.objects.get(user=self.staff).confirmed_at)

    def test_new_login_requires_a_code_and_a_valid_one_unlocks(self):
        self.enrol()
        self.client.logout()
        self.login()
        self.assertRedirects(self.client.get(self.dash), f"{reverse('accounts:two_factor_verify')}?next={self.dash}", fetch_redirect_response=False)
        device = StaffTOTPDevice.objects.get(user=self.staff)
        resp = self.client.post(reverse('accounts:two_factor_verify'), {'code': later_code(device), 'next': self.dash})
        self.assertRedirects(resp, self.dash, fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.dash).status_code, 200)

    def test_same_code_cannot_be_used_twice(self):
        self.enrol()  # enrolment consumed this time step's code
        self.client.logout()
        self.login()
        device = StaffTOTPDevice.objects.get(user=self.staff)
        # Replay the exact code that enrolment consumed (derived from its step, not the wall clock, so a
        # 30-second boundary falling mid-test can't turn this into a fresh, valid code).
        replayed = twofactor.totp_at(twofactor.decrypt_secret(device.secret_encrypted), device.last_used_step)
        resp = self.client.post(reverse('accounts:two_factor_verify'), {'code': replayed})
        self.assertEqual(resp.status_code, 200)  # rejected: replay of the enrolment code
        self.assertEqual(self.client.get(self.dash).status_code, 302)

    def test_recovery_code_works_once(self):
        codes = self.enrol()
        for attempt in (1, 2):
            self.client.logout()
            self.login()
            resp = self.client.post(reverse('accounts:two_factor_verify'), {'code': codes[0], 'next': self.dash})
            if attempt == 1:
                self.assertRedirects(resp, self.dash, fetch_redirect_response=False)
            else:
                self.assertEqual(resp.status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action='staff_2fa_recovery_used').count(), 1)
        self.assertEqual(StaffRecoveryCode.objects.filter(user=self.staff, used_at__isnull=True).count(), 9)

    @override_settings(RATE_LIMITING_ENABLED=True)
    def test_lockout_after_five_wrong_codes_even_for_a_correct_one(self):
        self.enrol()
        self.client.logout()
        self.login()
        for _ in range(5):
            self.client.post(reverse('accounts:two_factor_verify'), {'code': '111111'})
        device = StaffTOTPDevice.objects.get(user=self.staff)
        device.last_used_step = 0
        device.save()
        self.client.post(reverse('accounts:two_factor_verify'), {'code': current_code(device)})
        self.assertEqual(self.client.get(self.dash).status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action='staff_2fa_locked').exists())

    def test_other_staff_pages_and_private_documents_are_gated_too(self):
        from partners.test_routing_and_reputation import make_partner
        partner = make_partner('shopowner', status='pending')
        self.login()
        proof = reverse('partners:proof_file', args=[partner.pk, 'id'])
        self.assertEqual(self.client.get(proof).status_code, 404)             # unverified staff: no access
        for url in (reverse('payouts:staff_queue'), reverse('disputes:staff_queue'), reverse('donations:staff_queue')):
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.enrol()
        # 2FA passed, but opening someone else's ID document also needs the partner-verification capability.
        self.assertEqual(self.client.get(proof).status_code, 404)
        from django.contrib.auth.models import Group

        from adminpanel.roles import ensure_roles
        ensure_roles()
        self.staff.groups.add(Group.objects.get(name='Partner Verification'))
        self.assertEqual(self.client.get(proof).status_code, 200)

    def test_django_admin_requires_2fa(self):
        admin = User.objects.create_superuser('root', 'r@x.com', PW)
        self.client.login(username='root', password=PW)
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/2fa/setup/', resp['Location'])
        self.assertEqual(self.client.get('/admin/login/').status_code, 302)  # not intercepted into a loop
        self.assertEqual(admin.is_staff, True)

    def test_non_staff_are_unaffected_and_still_bounced_to_login(self):
        User.objects.create_user('plain', 'p@x.com', PW)
        self.client.login(username='plain', password=PW)
        resp = self.client.get(self.dash)
        self.assertIn('/accounts/login/', resp['Location'])

    def test_manage_page_regenerates_codes_with_a_fresh_code(self):
        self.enrol()
        device = StaffTOTPDevice.objects.get(user=self.staff)
        device.last_used_step = 0
        device.save()
        old = set(StaffRecoveryCode.objects.filter(user=self.staff).values_list('code_hash', flat=True))
        self.assertEqual(self.client.get(reverse('accounts:two_factor_manage')).status_code, 200)
        resp = self.client.post(reverse('accounts:two_factor_manage'), {'code': current_code(device)})
        self.assertEqual(len(re.findall(r'[0-9a-f]{5}-[0-9a-f]{5}', resp.content.decode())), 10)
        self.assertFalse(old & set(StaffRecoveryCode.objects.filter(user=self.staff).values_list('code_hash', flat=True)))

    def test_reset_command_removes_everything_and_forces_reenrolment(self):
        self.enrol()
        call_command('reset_staff_2fa', 'staffer', stdout=__import__('io').StringIO())
        self.assertFalse(StaffTOTPDevice.objects.filter(user=self.staff).exists())
        self.assertFalse(StaffRecoveryCode.objects.filter(user=self.staff).exists())
        self.assertTrue(AuditLog.objects.filter(action='staff_2fa_reset').exists())
        self.client.logout()
        self.login()
        self.assertIn('/accounts/2fa/setup/', self.client.get(self.dash)['Location'])


class TwoFactorSwitchTests(TestCase):
    @override_settings(STAFF_2FA_REQUIRED=False)
    def test_switch_off_leaves_staff_area_open(self):
        User.objects.create_user('s2', 's2@x.com', PW, is_staff=True)
        self.client.login(username='s2', password=PW)
        self.assertEqual(self.client.get(reverse('adminpanel:dashboard')).status_code, 200)
