import io

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User
from books.models import Book, Category
from chatapp.models import Conversation, Message


class LoginLockoutTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='victim', password='right-pass-123', email='v@example.com')
        self.user.email_verified = True
        self.user.save()
        self.url = reverse('accounts:login')

    def _try(self, pw, username='victim'):
        return self.client.post(self.url, {'username': username, 'password': pw})

    def test_account_locks_after_repeated_failures_even_with_right_password(self):
        for _ in range(5):
            self.assertEqual(self._try('wrong').status_code, 200)
        locked = self._try('right-pass-123')
        self.assertEqual(locked.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_login_clears_the_counter(self):
        for _ in range(3):
            self._try('wrong')
        self.assertEqual(self._try('right-pass-123').status_code, 302)
        self.client.logout()
        for _ in range(4):
            self.assertEqual(self._try('wrong').status_code, 200)  # budget is fresh, not 3+4

    def test_other_accounts_are_unaffected_by_one_lockout(self):
        for _ in range(5):
            self._try('wrong')
        other = User.objects.create_user(username='other', password='pw-other-123')
        other.email_verified = True
        other.save()
        self.assertEqual(self._try('pw-other-123', username='other').status_code, 302)


class OtpSendLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='otpuser', password='pw-12345', email='o@example.com')
        self.client.login(username='otpuser', password='pw-12345')

    def test_otp_requests_are_capped_per_hour(self):
        url = reverse('accounts:send_email_otp')
        codes = [self.client.post(url).status_code for _ in range(7)]
        self.assertEqual(codes[:5], [302] * 5)
        self.assertEqual(codes[5:], [429, 429])


def _png(size=(10, 10)):
    buf = io.BytesIO()
    Image.new('RGB', size, 'red').save(buf, 'PNG')
    return buf.getvalue()


class ChatUploadTests(TestCase):
    def setUp(self):
        cache.clear()
        seller = User.objects.create_user(username='cseller', password='pw')
        self.buyer = User.objects.create_user(username='cbuyer', password='pw')
        cat, _ = Category.objects.get_or_create(name='Test Category')
        book = Book.objects.create(seller=seller, category=cat, title='B', price=100, status='available')
        self.convo = Conversation.objects.create(book=book, buyer=self.buyer, seller=seller)
        self.client.login(username='cbuyer', password='pw')
        self.url = reverse('chatapp:conversation', args=[self.convo.pk])

    def test_real_image_is_accepted(self):
        self.client.post(self.url, {'text': 'hi', 'image': SimpleUploadedFile('p.png', _png(), 'image/png')})
        self.assertEqual(Message.objects.count(), 1)
        self.assertTrue(Message.objects.get().image)

    def test_fake_image_is_rejected(self):
        self.client.post(self.url, {'text': 'hi', 'image': SimpleUploadedFile('p.png', b'MZ\x90 not an image', 'image/png')})
        self.assertEqual(Message.objects.count(), 0)

    def test_oversized_image_is_rejected(self):
        big = _png() + b'\0' * (6 * 1024 * 1024)
        self.client.post(self.url, {'image': SimpleUploadedFile('p.png', big, 'image/png')})
        self.assertEqual(Message.objects.count(), 0)

    def test_image_flood_is_capped(self):
        for _ in range(12):
            self.client.post(self.url, {'image': SimpleUploadedFile('p.png', _png(), 'image/png')})
        self.assertEqual(Message.objects.count(), 10)

    def test_very_long_text_is_rejected(self):
        self.client.post(self.url, {'text': 'x' * 5000})
        self.assertEqual(Message.objects.count(), 0)
