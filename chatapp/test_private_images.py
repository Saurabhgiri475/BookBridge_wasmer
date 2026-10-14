"""Chat photos are private: stored outside public media, no public URL, and only the two
people in the conversation can fetch them."""
import io
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User
from books.models import Book, Category

from .models import Conversation, Message

_PUBLIC = tempfile.mkdtemp(prefix='bb-chat-public-')
_PRIVATE = tempfile.mkdtemp(prefix='bb-chat-private-')


def _png():
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), 'red').save(buf, 'PNG')
    return buf.getvalue()


@override_settings(MEDIA_ROOT=_PUBLIC, PRIVATE_MEDIA_ROOT=_PRIVATE)
class ChatImagePrivacyTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_PUBLIC, ignore_errors=True)
        shutil.rmtree(_PRIVATE, ignore_errors=True)

    def setUp(self):
        self.seller = User.objects.create_user(username='cs', password='pw')
        self.buyer = User.objects.create_user(username='cb', password='pw')
        self.outsider = User.objects.create_user(username='co', password='pw')
        self.staff = User.objects.create_user(username='cstaff', password='pw', is_staff=True)
        cat, _ = Category.objects.get_or_create(name='Test Category')
        book = Book.objects.create(seller=self.seller, category=cat, title='B', price=100)
        self.convo = Conversation.objects.create(book=book, buyer=self.buyer, seller=self.seller)
        self.msg = Message(conversation=self.convo, sender=self.buyer, text='pic')
        self.msg.image.save('p.png', ContentFile(_png()), save=True)
        self.url = reverse('chatapp:image', args=[self.msg.pk])

    def test_file_is_stored_privately_and_has_no_public_url(self):
        self.assertTrue(self.msg.image.path.startswith(_PRIVATE))
        with self.assertRaises(ValueError):
            self.msg.image.url

    def test_both_participants_can_view(self):
        for name in ('cb', 'cs'):
            self.client.login(username=name, password='pw')
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
            self.assertIn('no-store', response['Cache-Control'])
            self.client.logout()

    def test_other_users_and_staff_get_404(self):
        for name in ('co', 'cstaff'):
            self.client.login(username=name, password='pw')
            self.assertEqual(self.client.get(self.url).status_code, 404, name)
            self.client.logout()

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_message_without_photo_is_404(self):
        plain = Message.objects.create(conversation=self.convo, sender=self.buyer, text='no pic')
        self.client.login(username='cb', password='pw')
        self.assertEqual(self.client.get(reverse('chatapp:image', args=[plain.pk])).status_code, 404)

    def test_conversation_page_links_to_the_protected_view(self):
        self.client.login(username='cb', password='pw')
        html = self.client.get(reverse('chatapp:conversation', args=[self.convo.pk])).content.decode()
        self.assertIn(self.url, html)
        self.assertNotIn('/media/chat_images', html)

    def test_django_admin_message_pages_render(self):
        admin = User.objects.create_superuser('adm', 'a@x.com', 'pw')
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('admin:chatapp_message_changelist')).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:chatapp_message_change', args=[self.msg.pk])).status_code, 200)
