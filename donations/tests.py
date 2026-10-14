from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from adminpanel.models import AuditLog
from notifications.models import Notification

from . import services
from .certificate import name_is_printable, render_certificate_pdf
from .models import Donation, DonationCertificate, DonationItem, DonationPartner


def make_partner(**kw):
    defaults = dict(name='Sunrise Trust', org_type='ngo', city='Kanpur', address='12 Mall Road', is_active=True)
    defaults.update(kw)
    return DonationPartner.objects.create(**defaults)


def make_donation(donor, partner, qty=3, status='submitted', name='Asha Verma'):
    d = Donation.objects.create(donor=donor, partner=partner, certificate_name=name, status=status)
    DonationItem.objects.create(donation=d, title='NCERT Physics 12', quantity=qty)
    return d


def form_data(partner, **extra):
    data = {
        'partner': partner.pk, 'certificate_name': 'Asha Verma', 'handover': 'dropoff',
        'pickup_address': '', 'pickup_city': '', 'pickup_pincode': '', 'contact_phone': '', 'note': '',
        'items-TOTAL_FORMS': 5, 'items-INITIAL_FORMS': 0, 'items-MIN_NUM_FORMS': 0, 'items-MAX_NUM_FORMS': 20,
        'items-0-title': 'NCERT Physics 12', 'items-0-author': 'NCERT', 'items-0-isbn': '', 'items-0-condition': 'good', 'items-0-quantity': 2,
        'items-1-title': 'RD Sharma 10', 'items-1-author': '', 'items-1-isbn': '', 'items-1-condition': 'fair', 'items-1-quantity': 1,
    }
    for i in range(2, 5):
        data.update({f'items-{i}-title': '', f'items-{i}-author': '', f'items-{i}-isbn': '', f'items-{i}-condition': 'good', f'items-{i}-quantity': 1})
    data.update(extra)
    return data


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', RATE_LIMITING_ENABLED=False)
class DonationBase(TestCase):
    def setUp(self):
        cache.clear()
        self.partner = make_partner()
        self.donor = User.objects.create_user('asha', 'asha@x.com', 'pw12345!')
        self.other = User.objects.create_user('ravi', 'ravi@x.com', 'pw12345!')
        self.staff = User.objects.create_user('boss', 'boss@x.com', 'pw12345!', is_staff=True, is_superuser=True)


class DonationSubmitTests(DonationBase):
    def test_login_required(self):
        r = self.client.post(reverse('donations:create'), form_data(self.partner))
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r.url)
        self.assertEqual(Donation.objects.count(), 0)

    def test_submit_creates_donation_items_and_notifies(self):
        self.client.login(username='asha', password='pw12345!')
        r = self.client.post(reverse('donations:create'), form_data(self.partner))
        d = Donation.objects.get()
        self.assertRedirects(r, reverse('donations:detail', args=[d.pk]))
        self.assertEqual((d.donor, d.status, d.claimed_count, d.items.count()), (self.donor, 'submitted', 3, 2))
        self.assertTrue(Notification.objects.filter(user=self.donor, title__icontains='received').exists())

    def test_needs_at_least_one_book(self):
        self.client.login(username='asha', password='pw12345!')
        data = form_data(self.partner, **{'items-0-title': '', 'items-0-author': '', 'items-0-quantity': 1, 'items-1-title': '', 'items-1-condition': 'good'})
        r = self.client.post(reverse('donations:create'), data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Add at least one book')
        self.assertEqual(Donation.objects.count(), 0)

    def test_total_copies_capped(self):
        self.client.login(username='asha', password='pw12345!')
        data = form_data(self.partner, **{'items-0-quantity': 300, 'items-1-quantity': 300})
        r = self.client.post(reverse('donations:create'), data)
        self.assertContains(r, 'up to 500 books')
        self.assertEqual(Donation.objects.count(), 0)

    def test_pickup_requires_partner_support_and_address(self):
        self.client.login(username='asha', password='pw12345!')
        r = self.client.post(reverse('donations:create'), form_data(self.partner, handover='pickup'))
        self.assertContains(r, 'does not accept pickups')
        self.partner.accepts_pickup = True
        self.partner.save()
        r = self.client.post(reverse('donations:create'), form_data(self.partner, handover='pickup'))
        self.assertContains(r, 'pickup address')
        r = self.client.post(reverse('donations:create'), form_data(
            self.partner, handover='pickup', pickup_address='5 Civil Lines', pickup_city='Kanpur',
            pickup_pincode='208001', contact_phone='9876543210'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Donation.objects.get().pickup_pincode, '208001')

    def test_inactive_partner_not_selectable(self):
        gone = make_partner(name='Closed Org', is_active=False)
        self.client.login(username='asha', password='pw12345!')
        r = self.client.post(reverse('donations:create'), form_data(gone))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Donation.objects.count(), 0)

    def test_non_latin_name_rejected_without_font(self):
        self.assertFalse(name_is_printable('आशा वर्मा'))
        self.assertTrue(name_is_printable("Zoë O'Brien-Ñandú"))
        self.client.login(username='asha', password='pw12345!')
        r = self.client.post(reverse('donations:create'), form_data(self.partner, certificate_name='आशा वर्मा'))
        self.assertContains(r, 'English (Latin) letters')

    def test_no_partners_redirects_home(self):
        DonationPartner.objects.update(is_active=False)
        self.client.login(username='asha', password='pw12345!')
        self.assertRedirects(self.client.get(reverse('donations:create')), reverse('donations:home'))


class DonationAccessTests(DonationBase):
    def test_only_donor_or_staff_can_view(self):
        d = make_donation(self.donor, self.partner)
        url = reverse('donations:detail', args=[d.pk])
        self.client.login(username='ravi', password='pw12345!')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.login(username='asha', password='pw12345!')
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.login(username='boss', password='pw12345!')
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_staff_pages_blocked_for_normal_users(self):
        d = make_donation(self.donor, self.partner)
        self.client.login(username='asha', password='pw12345!')
        for name, args in (('donations:staff_queue', []), ('donations:staff_detail', [d.pk])):
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302)
        r = self.client.post(reverse('donations:staff_receive', args=[d.pk]), {'received_count': 3})
        self.assertEqual(r.status_code, 302)
        d.refresh_from_db()
        self.assertEqual(d.status, 'submitted')
        self.assertFalse(DonationCertificate.objects.exists())

    def test_cancel_only_by_donor_and_only_while_open(self):
        d = make_donation(self.donor, self.partner)
        self.client.login(username='ravi', password='pw12345!')
        self.assertEqual(self.client.post(reverse('donations:cancel', args=[d.pk])).status_code, 404)
        self.client.login(username='asha', password='pw12345!')
        self.client.post(reverse('donations:cancel', args=[d.pk]))
        d.refresh_from_db()
        self.assertEqual(d.status, 'cancelled')
        d2 = make_donation(self.donor, self.partner, status='received')
        self.client.post(reverse('donations:cancel', args=[d2.pk]))
        d2.refresh_from_db()
        self.assertEqual(d2.status, 'received')


class StaffWorkflowTests(DonationBase):
    def setUp(self):
        super().setUp()
        self.d = make_donation(self.donor, self.partner, qty=5)
        self.client.login(username='boss', password='pw12345!')

    def test_accept_notifies_and_audits(self):
        self.client.post(reverse('donations:staff_accept', args=[self.d.pk]))
        self.d.refresh_from_db()
        self.assertEqual((self.d.status, self.d.reviewed_by), ('accepted', self.staff))
        self.assertTrue(Notification.objects.filter(user=self.donor, title__icontains='accepted').exists())
        self.assertTrue(AuditLog.objects.filter(action='accept_donation', actor=self.staff).exists())

    def test_cannot_accept_twice(self):
        self.client.post(reverse('donations:staff_accept', args=[self.d.pk]))
        self.client.post(reverse('donations:staff_accept', args=[self.d.pk]))
        self.assertEqual(AuditLog.objects.filter(action='accept_donation').count(), 1)

    def test_reject_needs_reason(self):
        self.client.post(reverse('donations:staff_reject', args=[self.d.pk]), {'reason': '  '})
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'submitted')
        self.client.post(reverse('donations:staff_reject', args=[self.d.pk]), {'reason': 'Books are damaged'})
        self.d.refresh_from_db()
        self.assertEqual((self.d.status, self.d.rejection_reason), ('rejected', 'Books are damaged'))
        self.assertTrue(Notification.objects.filter(user=self.donor, body__icontains='damaged').exists())

    def test_receive_issues_certificate_for_verified_count_not_claimed(self):
        mail.outbox.clear()
        r = self.client.post(reverse('donations:staff_receive', args=[self.d.pk]), {'received_count': 4})
        self.assertEqual(r.status_code, 302)
        self.d.refresh_from_db()
        cert = self.d.certificate
        self.assertEqual((self.d.status, self.d.received_count, cert.books_count), ('received', 4, 4))  # donor listed 5
        self.assertEqual((cert.recipient_name, cert.partner_name), ('Asha Verma', 'Sunrise Trust'))
        self.assertRegex(cert.code, r'^BBD-[A-Z2-9]{10}$')
        # one email, with the PDF attached
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments[0][2], 'application/pdf')
        self.assertTrue(mail.outbox[0].attachments[0][1].startswith(b'%PDF'))
        self.assertTrue(Notification.objects.filter(user=self.donor, title__icontains='certificate').exists())
        self.assertTrue(AuditLog.objects.filter(action='issue_certificate').exists())

    def test_receive_twice_issues_only_one_certificate(self):
        url = reverse('donations:staff_receive', args=[self.d.pk])
        self.client.post(url, {'received_count': 5})
        self.client.post(url, {'received_count': 9})
        self.assertEqual(DonationCertificate.objects.count(), 1)
        self.assertEqual(DonationCertificate.objects.get().books_count, 5)

    def test_receive_rejects_zero_and_closed_donations(self):
        self.client.post(reverse('donations:staff_receive', args=[self.d.pk]), {'received_count': 0})
        self.assertFalse(DonationCertificate.objects.exists())
        self.d.status = 'cancelled'
        self.d.save()
        self.client.post(reverse('donations:staff_receive', args=[self.d.pk]), {'received_count': 3})
        self.assertFalse(DonationCertificate.objects.exists())

    def test_revoke(self):
        cert = services.issue_certificate(self.d, self.staff, 5)
        self.client.post(reverse('donations:staff_revoke', args=[cert.code]), {'reason': 'Issued in error'})
        cert.refresh_from_db()
        self.assertTrue(cert.revoked)
        self.assertEqual(services.books_donated_total(), 0)
        self.assertTrue(AuditLog.objects.filter(action='revoke_certificate').exists())

    def test_queue_lists_open_and_filters(self):
        make_donation(self.donor, self.partner, status='rejected')
        r = self.client.get(reverse('donations:staff_queue'))
        self.assertEqual(len(r.context['page']), 1)
        r = self.client.get(reverse('donations:staff_queue') + '?status=all')
        self.assertEqual(len(r.context['page']), 2)
        r = self.client.get(reverse('donations:staff_queue') + '?q=asha&status=all')
        self.assertEqual(len(r.context['page']), 2)


class CertificateTests(DonationBase):
    def setUp(self):
        super().setUp()
        self.d = make_donation(self.donor, self.partner, qty=3)
        self.cert = services.issue_certificate(self.d, self.staff, 3)

    def test_pdf_download_only_for_owner_or_staff(self):
        url = reverse('donations:certificate_pdf', args=[self.cert.code])
        self.assertEqual(self.client.get(url).status_code, 302)  # anonymous -> login
        self.client.login(username='ravi', password='pw12345!')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.login(username='asha', password='pw12345!')
        r = self.client.get(url)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))
        self.assertIn(self.cert.code, r['Content-Disposition'])

    def test_revoked_certificate_cannot_be_downloaded(self):
        services.revoke_certificate(self.cert, 'mistake')
        self.client.login(username='asha', password='pw12345!')
        r = self.client.get(reverse('donations:certificate_pdf', args=[self.cert.code]))
        self.assertEqual(r.status_code, 302)

    def test_pdf_renders_edge_cases(self):
        self.cert.recipient_name = 'A Very Long Donor Name ' * 4
        self.cert.books_count = 1
        self.assertTrue(render_certificate_pdf(self.cert).startswith(b'%PDF'))
        self.cert.revoked = True
        self.assertTrue(render_certificate_pdf(self.cert).startswith(b'%PDF'))

    def test_public_verification(self):
        r = self.client.get(reverse('donations:verify', args=[self.cert.code]))
        self.assertContains(r, 'Valid certificate')
        self.assertContains(r, 'Asha Verma')
        self.assertContains(r, 'Sunrise Trust')
        # lower-case / spaced input from the box still resolves
        r = self.client.get(reverse('donations:verify_form') + '?code=' + self.cert.code.lower())
        self.assertEqual(r.status_code, 302)
        self.assertContains(self.client.get(r.url), 'Valid certificate')

    def test_verification_does_not_leak_private_data(self):
        r = self.client.get(reverse('donations:verify', args=[self.cert.code]))
        for secret in ('asha@x.com', 'asha</', self.donor.email):
            self.assertNotContains(r, secret)

    def test_verification_unknown_and_revoked(self):
        self.assertContains(self.client.get(reverse('donations:verify', args=['BBD-NOPE'])), 'No certificate found')
        services.revoke_certificate(self.cert, 'mistake')
        self.assertContains(self.client.get(reverse('donations:verify', args=[self.cert.code])), 'revoked')

    def test_codes_unique_and_random(self):
        d2 = make_donation(self.other, self.partner)
        c2 = services.issue_certificate(d2, self.staff, 1)
        self.assertNotEqual(self.cert.code, c2.code)


class ImpactIntegrationTests(DonationBase):
    def test_only_valid_certificates_count(self):
        d1 = make_donation(self.donor, self.partner)
        services.issue_certificate(d1, self.staff, 7)
        make_donation(self.donor, self.partner, qty=100)  # not received yet -> not counted
        c2 = services.issue_certificate(make_donation(self.other, self.partner), self.staff, 3)
        self.assertEqual(services.books_donated_total(), 10)
        self.assertEqual(services.books_donated_by(self.donor), 7)
        services.revoke_certificate(c2, 'x')
        self.assertEqual(services.books_donated_total(), 7)
        stats = services.donation_stats()
        self.assertEqual((stats['books_donated'], stats['donor_count'], stats['org_count']), (7, 1, 1))

    def test_sustainability_page_includes_donations(self):
        services.issue_certificate(make_donation(self.donor, self.partner), self.staff, 130)
        r = self.client.get(reverse('books:sustainability'))
        self.assertEqual(r.context['books_reused'], 130)
        self.assertEqual(r.context['giving']['books_donated'], 130)
        self.assertAlmostEqual(r.context['trees_saved'], 1.0)
        self.assertContains(r, 'Book Donations')

    def test_profile_counts_donated_books(self):
        services.issue_certificate(make_donation(self.donor, self.partner), self.staff, 6)
        self.client.login(username='asha', password='pw12345!')
        r = self.client.get(reverse('accounts:profile'))
        self.assertEqual(r.context['books_donated'], 6)
        self.assertEqual(r.context['impact']['books_reused'], 6)
        self.assertTrue(any(b['code'] == 'eco_starter' for b in r.context['earned_badges']))  # 5+ books

    def test_public_pages_render(self):
        self.assertEqual(self.client.get(reverse('donations:home')).status_code, 200)
        self.assertEqual(self.client.get(reverse('donations:verify_form')).status_code, 200)
        self.assertEqual(self.client.get(reverse('books:home')).status_code, 200)
        self.client.login(username='asha', password='pw12345!')
        for name in ('donations:create', 'donations:mine'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_staff_dashboard_shows_pending_count(self):
        make_donation(self.donor, self.partner)
        self.client.login(username='boss', password='pw12345!')
        r = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(r.context['stats']['pending_donation_count'], 1)
