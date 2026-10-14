"""Buyback photos are private: only the seller, a partner who may see that lead, and staff can open them."""
import io
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User

from .models import BuybackRequestImage
from .test_routing_and_reputation import make_lead, make_partner

_PUBLIC = tempfile.mkdtemp(prefix='bb-bb-public-')
_PRIVATE = tempfile.mkdtemp(prefix='bb-bb-private-')


def _png():
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), 'blue').save(buf, 'PNG')
    return buf.getvalue()


@override_settings(MEDIA_ROOT=_PUBLIC, PRIVATE_MEDIA_ROOT=_PRIVATE)
class BuybackImagePrivacyTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_PUBLIC, ignore_errors=True)
        shutil.rmtree(_PRIVATE, ignore_errors=True)

    def setUp(self):
        self.seller = User.objects.create_user(username='bbseller', password='pw')
        self.stranger = User.objects.create_user(username='bbstranger', password='pw')
        self.partner = make_partner('bbpartner')
        self.lead = make_lead(self.seller)
        self.image = BuybackRequestImage(buyback_request=self.lead)
        self.image.image.save('book.png', ContentFile(_png()), save=True)
        self.url = reverse('partners:buyback_image', args=[self.image.pk])

    def test_file_is_private(self):
        self.assertTrue(self.image.image.path.startswith(_PRIVATE))
        with self.assertRaises(ValueError):
            self.image.image.url

    def test_seller_can_view(self):
        self.client.login(username='bbseller', password='pw')
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_open_pending_lead_is_visible_to_an_approved_partner(self):
        self.client.login(username='bbpartner', password='pw12345!')
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_lead_reserved_for_another_partner_is_404_for_this_one(self):
        other = make_partner('bbother')
        self.lead.assigned_partner = other
        self.lead.save()
        self.client.login(username='bbpartner', password='pw12345!')
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_stranger_gets_404_and_anonymous_is_sent_to_login(self):
        self.client.login(username='bbstranger', password='pw')
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_pages_use_the_protected_url(self):
        self.client.login(username='bbseller', password='pw')
        html = self.client.get(reverse('partners:buyback_detail', args=[self.lead.pk])).content.decode()
        self.assertIn(self.url, html)
        self.assertNotIn('/media/buyback_requests', html)

    def test_django_admin_buyback_page_renders(self):
        admin = User.objects.create_superuser('bbadm', 'a@x.com', 'pw')
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('admin:partners_buybackrequest_change', args=[self.lead.pk])).status_code, 200)
