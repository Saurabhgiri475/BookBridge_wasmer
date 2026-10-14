import io
import shutil
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User
from adminpanel.models import AuditLog
from books.models import Book, Category

from .test_routing_and_reputation import make_partner

_PUBLIC = tempfile.mkdtemp(prefix='bb-pf-public-')
_PRIVATE = tempfile.mkdtemp(prefix='bb-pf-private-')


def tearDownModule():
    shutil.rmtree(_PUBLIC, ignore_errors=True)
    shutil.rmtree(_PRIVATE, ignore_errors=True)


def _png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(buf, 'PNG')
    return buf.getvalue()


@override_settings(MEDIA_ROOT=_PUBLIC, PRIVATE_MEDIA_ROOT=_PRIVATE)
class PrivateProofTests(TestCase):
    def setUp(self):
        self.partner = make_partner('shopowner', status='pending')
        self.stranger = User.objects.create_user('stranger', 's@x.com', 'pw12345!')
        self.staff = User.objects.create_user('staffer', 'st@x.com', 'pw12345!', is_staff=True, is_superuser=True)
        self.id_url = reverse('partners:proof_file', args=[self.partner.pk, 'id'])
        self.shop_url = reverse('partners:proof_file', args=[self.partner.pk, 'shop'])

    def test_files_are_stored_outside_public_media(self):
        self.assertTrue((Path(_PRIVATE) / self.partner.owner_id_proof.name).exists())
        self.assertFalse((Path(_PUBLIC) / self.partner.owner_id_proof.name).exists())

    def test_private_files_have_no_public_url(self):
        with self.assertRaises(ValueError):
            self.partner.owner_id_proof.url

    def test_anonymous_is_sent_to_login(self):
        resp = self.client.get(self.id_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_other_users_get_404(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.id_url).status_code, 404)
        self.assertEqual(self.client.get(self.shop_url).status_code, 404)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_owner_can_view_own_proof_without_audit_entry(self):
        self.client.force_login(self.partner.user)
        resp = self.client.get(self.id_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/png')
        self.assertEqual(resp['Cache-Control'], 'private, no-store')
        self.assertEqual(resp['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_staff_can_view_and_access_is_logged(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.shop_url)
        self.assertEqual(resp.status_code, 200)
        entry = AuditLog.objects.get()
        self.assertEqual((entry.actor, entry.action, entry.target_id), (self.staff, 'view_partner_proof', self.partner.pk))

    def test_unknown_kind_and_missing_file_are_404(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('partners:proof_file', args=[self.partner.pk, 'passport'])).status_code, 404)
        (Path(_PRIVATE) / self.partner.owner_id_proof.name).unlink()
        self.assertEqual(self.client.get(self.id_url).status_code, 404)

    def test_staff_dashboard_links_to_protected_view(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(resp, self.id_url)
        self.assertNotContains(resp, '/media/partners/')

    def test_django_admin_change_page_renders(self):
        admin = User.objects.create_superuser('root', 'r@x.com', 'pw12345!')
        self.client.force_login(admin)
        resp = self.client.get(reverse('admin:partners_shopkeeperpartner_change', args=[self.partner.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.id_url)

    def test_apply_page_preview_uses_protected_url(self):
        self.client.force_login(self.partner.user)
        resp = self.client.get(reverse('partners:apply'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '/media/partners/')


@override_settings(MEDIA_ROOT=_PUBLIC, PRIVATE_MEDIA_ROOT=_PRIVATE)
class DigitalFileAndMoveCommandTests(TestCase):
    def test_digital_file_is_private_and_admin_page_renders(self):
        seller = User.objects.create_user('seller', 'sel@x.com', 'pw12345!')
        cat = Category.objects.create(name='Notes')
        book = Book(title='Notes PDF', seller=seller, category=cat, price=10, listing_type='digital', digital_type='pdf_notes')
        book.digital_file.save('notes.pdf', ContentFile(b'%PDF-1.4 test'), save=False)
        book.save()
        self.assertTrue((Path(_PRIVATE) / book.digital_file.name).exists())
        self.assertFalse((Path(_PUBLIC) / book.digital_file.name).exists())
        admin = User.objects.create_superuser('root2', 'r2@x.com', 'pw12345!')
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('admin:books_book_change', args=[book.pk])).status_code, 200)

    def test_move_command_relocates_legacy_public_files(self):
        partner = make_partner('legacy')
        name = partner.owner_id_proof.name
        private_copy = Path(_PRIVATE) / name
        legacy = Path(_PUBLIC) / name
        legacy.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(private_copy), str(legacy))  # simulate a file uploaded before private storage existed
        call_command('move_private_files', '--dry-run', stdout=io.StringIO())
        self.assertTrue(legacy.exists() and not private_copy.exists())
        call_command('move_private_files', stdout=io.StringIO())
        self.assertTrue(private_copy.exists() and not legacy.exists())
