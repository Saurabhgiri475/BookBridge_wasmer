import re
from datetime import datetime, timedelta
from unittest import mock

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from notifications.models import Notification

LOCMEM = override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
NEW_PW = 'Tr1cky-New-Pass-77'


def reset_link(message):
    match = re.search(r'https?://\S+/accounts/reset/\S+/\S+/', message.body)
    assert match, message.body
    return match.group(0)


@LOCMEM
class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('asha', 'asha@example.com', 'Old-Pass-12345')
        self.url = reverse('accounts:password_reset')

    def request_reset(self, email='asha@example.com', **extra):
        return self.client.post(self.url, {'email': email}, **extra)

    def test_login_page_links_to_reset_and_shows_support_contact(self):
        html = self.client.get(reverse('accounts:login')).content.decode()
        self.assertIn(self.url, html)
        self.assertIn('tel:+917985486802', html)
        self.assertIn('saurabhgiri95893@gmail.com', html)

    def test_full_flow_end_to_end(self):
        resp = self.request_reset()
        self.assertRedirects(resp, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ['asha@example.com'])
        self.assertIn('Reset your BookBridge password', msg.subject)
        self.assertIn('+91 79854 86802', msg.body)
        self.assertIn('saurabhgiri95893@gmail.com', msg.body)

        link = reset_link(msg)
        page = self.client.get(link, follow=True)
        self.assertContains(page, 'Choose a new password')
        post_url = page.redirect_chain[-1][0]
        done = self.client.post(post_url, {'new_password1': NEW_PW, 'new_password2': NEW_PW})
        self.assertRedirects(done, reverse('accounts:password_reset_complete'))

        self.assertFalse(self.client.login(username='asha', password='Old-Pass-12345'))
        self.assertTrue(self.client.login(username='asha', password=NEW_PW))

    def test_link_works_only_once(self):
        self.request_reset()
        link = reset_link(mail.outbox[0])
        post_url = self.client.get(link).url
        self.client.post(post_url, {'new_password1': NEW_PW, 'new_password2': NEW_PW})
        again = self.client.get(link, follow=True)
        self.assertContains(again, "isn't valid any more")

    def test_garbage_token_shows_invalid_page_with_new_link_button(self):
        page = self.client.get(reverse('accounts:password_reset_confirm', args=['MQ', 'bad-token']))
        self.assertContains(page, "isn't valid any more")
        self.assertContains(page, self.url)

    def test_expired_link_is_rejected(self):
        self.request_reset()
        link = reset_link(mail.outbox[0])
        with mock.patch.object(PasswordResetTokenGenerator, '_now', return_value=datetime.now() + timedelta(hours=2)):
            self.assertContains(self.client.get(link, follow=True), "isn't valid any more")

    def test_default_link_lifetime_is_one_hour(self):
        from django.conf import settings
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 3600)

    def test_unknown_email_looks_identical_and_sends_nothing(self):
        resp = self.request_reset('nobody@example.com')
        self.assertRedirects(resp, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_email_match_is_case_insensitive(self):
        self.request_reset('ASHA@Example.COM')
        self.assertEqual(len(mail.outbox), 1)

    def test_unverified_or_inactive_account_gets_no_email(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.request_reset()
        self.assertEqual(len(mail.outbox), 0)

    def test_weak_password_is_rejected(self):
        self.request_reset()
        post_url = self.client.get(reset_link(mail.outbox[0])).url
        for bad in ('12345678', 'password', 'short1'):
            resp = self.client.post(post_url, {'new_password1': bad, 'new_password2': bad})
            self.assertEqual(resp.status_code, 200, bad)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password('Old-Pass-12345'))

    def test_mismatched_passwords_rejected(self):
        self.request_reset()
        post_url = self.client.get(reset_link(mail.outbox[0])).url
        resp = self.client.post(post_url, {'new_password1': NEW_PW, 'new_password2': NEW_PW + 'x'})
        self.assertEqual(resp.status_code, 200)

    def test_per_email_cap_sends_no_more_mail_but_looks_the_same(self):
        for _ in range(3):
            self.request_reset()
        self.assertEqual(len(mail.outbox), 3)
        resp = self.request_reset()
        self.assertRedirects(resp, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(PASSWORD_RESET_MAX_PER_IP_PER_HOUR=2)
    def test_per_network_cap_returns_429(self):
        self.request_reset('a@example.com')
        self.request_reset('b@example.com')
        resp = self.request_reset('c@example.com')
        self.assertEqual(resp.status_code, 429)
        self.assertContains(resp, 'Too many reset requests', status_code=429)

    def test_reset_clears_login_lockout_and_signs_out_other_sessions(self):
        for _ in range(5):
            self.client.post(reverse('accounts:login'), {'username': 'asha', 'password': 'wrong'})
        locked = self.client.post(reverse('accounts:login'), {'username': 'asha', 'password': 'Old-Pass-12345'})
        self.assertEqual(locked.status_code, 429)

        other = self.client_class()
        other.force_login(self.user)
        self.assertEqual(other.get(reverse('accounts:profile')).status_code, 200)

        self.request_reset()
        post_url = self.client.get(reset_link(mail.outbox[0])).url
        self.client.post(post_url, {'new_password1': NEW_PW, 'new_password2': NEW_PW})

        ok = self.client.post(reverse('accounts:login'), {'username': 'asha', 'password': NEW_PW})
        self.assertEqual(ok.status_code, 302)  # lockout gone
        self.assertEqual(other.get(reverse('accounts:profile')).status_code, 302)  # old session kicked out

    def test_owner_is_told_the_password_changed(self):
        self.request_reset()
        post_url = self.client.get(reset_link(mail.outbox[0])).url
        mail.outbox.clear()
        self.client.post(post_url, {'new_password1': NEW_PW, 'new_password2': NEW_PW})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('password was changed', mail.outbox[0].subject.lower())
        self.assertIn('saurabhgiri95893@gmail.com', mail.outbox[0].body)
        self.assertTrue(Notification.objects.filter(user=self.user, title='Your password was changed').exists())


class SupportContactTests(TestCase):
    def test_footer_shows_contact_on_every_page_in_two_equal_halves(self):
        html = self.client.get(reverse('books:about')).content.decode()
        footer = html[html.index('<footer'):html.index('</footer>')]
        self.assertEqual(footer.count('col-md-6'), 2)
        # the help block hugs the right edge on desktop (text-md-end) and stays left-aligned when stacked on phones
        self.assertIn('col-md-6 text-md-end bb-footer-help', footer)
        self.assertIn('tel:+917985486802', footer)
        self.assertIn('+91 79854 86802', footer)
        self.assertIn('mailto:saurabhgiri95893@gmail.com', footer)

    def test_contact_page(self):
        resp = self.client.get(reverse('books:contact'))
        self.assertContains(resp, 'tel:+917985486802')
        self.assertContains(resp, 'saurabhgiri95893@gmail.com')

    @override_settings(SUPPORT_PHONE='+91 98765 43210', SUPPORT_EMAIL='help@example.org')
    def test_contact_details_come_from_settings(self):
        html = self.client.get(reverse('books:contact')).content.decode()
        self.assertIn('tel:+919876543210', html)
        self.assertIn('help@example.org', html)
        self.assertNotIn('7985486802', html)
