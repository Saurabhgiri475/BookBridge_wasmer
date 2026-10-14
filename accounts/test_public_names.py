"""Email-style usernames must never be shown to other people.

Old accounts may use their email address as the username. The public_name filter masks it on
every page where another person's name appears, book links use the user id (not the username),
and signup no longer accepts "@" in a username.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.forms import SignUpForm
from accounts.templatetags.public_names import public_name
from books.models import Book, Category

User = get_user_model()
EMAIL_NAME = 'someone.private@example.com'


class PublicNameFilterTests(TestCase):
    def test_plain_username_is_unchanged(self):
        self.assertEqual(public_name('riya_k'), 'riya_k')

    def test_email_username_is_masked(self):
        self.assertEqual(public_name(EMAIL_NAME), 'som***')
        self.assertNotIn('@', public_name(EMAIL_NAME))

    def test_short_local_part_and_empty_values(self):
        self.assertEqual(public_name('ab@example.com'), 'a***')
        self.assertEqual(public_name(None), '')

    def test_accepts_a_user_object(self):
        user = User.objects.create_user(username=EMAIL_NAME, password='pw-12345-xyz', email='x@example.com')
        self.assertEqual(public_name(user), 'som***')


class BookPageDoesNotLeakEmailTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username=EMAIL_NAME, password='pw-12345-xyz', email=EMAIL_NAME)
        category, _ = Category.objects.get_or_create(name='Leak Test')
        self.book = Book.objects.create(seller=self.seller, category=category, title='Leak Book', price=100,
                                        status='available')

    def test_logged_out_visitor_sees_masked_name_and_id_link(self):
        resp = self.client.get(reverse('books:detail', args=[self.book.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, EMAIL_NAME)
        self.assertContains(resp, 'som***')
        self.assertContains(resp, reverse('accounts:profile_by_id', args=[self.seller.pk]))

    def test_profile_by_id_needs_login_and_then_shows_the_profile(self):
        url = reverse('accounts:profile_by_id', args=[self.seller.pk])
        self.assertEqual(self.client.get(url).status_code, 302)  # login required, like the username URL
        viewer = User.objects.create_user(username='viewer', password='pw-12345-xyz', email='v@example.com')
        self.client.force_login(viewer)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, EMAIL_NAME)


class SignupUsernameTests(TestCase):
    def _form(self, username):
        return SignUpForm(data={
            'username': username, 'email': 'new@example.com', 'role': 'buyer', 'phone': '9876543210',
            'city': 'Kanpur', 'password1': 'pw-12345-xyz-long', 'password2': 'pw-12345-xyz-long',
        })

    def test_email_style_username_is_rejected(self):
        form = self._form('new@example.com')
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_plain_username_passes_the_username_check(self):
        form = self._form('plain_name')
        form.is_valid()
        self.assertNotIn('username', form.errors)
