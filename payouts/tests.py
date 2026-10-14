from datetime import datetime, timezone as dt_tz
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from accounts.models import User
from adminpanel.models import AuditLog
from books.models import Book, Category
from notifications.models import Notification
from payouts.models import PayoutProfile
from payouts.namecheck import names_match
from orders.models import Order, Payment, Shipment
from partners.models import BuybackRequest, PartnerCommission
from partners.tests import _make_partner

from . import services, statements
from .models import Payout
from .upi import is_valid_upi_id, mask_upi_id, upi_deeplink

LOCMEM = override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')


def make_order(seller, buyer, price=1000, title='Some Book', payment_status='released', order_status='delivered'):
    category, _ = Category.objects.get_or_create(name='Test Category')
    book = Book.objects.create(seller=seller, category=category, title=title, price=price)
    order = Order.objects.create(
        buyer=buyer, book=book, final_price=price, delivery_charge=40, payment_method='upi', status=order_status,
        delivery_address='1 MG Road', delivery_city='Kanpur', delivery_pincode='208001',
    )
    Payment.objects.create(order=order, amount=order.total_amount, method='upi', status=payment_status)
    return order


class UpiHelperTests(TestCase):
    def test_validation(self):
        for good in ('rahul@okhdfcbank', 'a.b-c_d@ybl', ' Rahul@OKSBI ', '9876543210@paytm'):
            self.assertTrue(is_valid_upi_id(good), good)
        for bad in ('', 'rahul', '@ybl', 'r@y', 'rahul@@ybl', 'ra hul@ybl', 'rahul@yb l', 'x@123'):
            self.assertFalse(is_valid_upi_id(bad), bad)

    def test_mask(self):
        self.assertEqual(mask_upi_id('rahul.sharma@okhdfcbank'), 'ra**********@okhdfcbank')
        self.assertEqual(mask_upi_id('nonsense'), '')

    def test_deeplink_escapes_hostile_names(self):
        link = upi_deeplink('a@ybl', 'Ra&hul #1', '90.00', 'BookBridge payout')
        self.assertIn('pa=a@ybl', link)
        self.assertIn('pn=Ra%26hul%20%231', link)
        self.assertNotIn('&hul', link)


class PayoutCreationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')

    def test_amounts_match_order_page(self):
        order = make_order(self.seller, self.buyer, price=999)
        payout = services.ensure_payout(order)
        order.refresh_from_db()  # a fresh Order has the float model default; the DB value is a Decimal
        self.assertEqual(payout.gross_amount, Decimal('999.00'))
        self.assertEqual(payout.commission_amount, order.commission_amount)
        self.assertEqual(payout.net_amount, order.seller_payout)
        self.assertEqual(payout.commission_amount + payout.net_amount, payout.gross_amount)
        self.assertEqual(payout.status, 'pending')

    def test_idempotent_and_snapshot(self):
        order = make_order(self.seller, self.buyer)
        first = services.ensure_payout(order)
        order.platform_commission_percent = Decimal('50.0')
        order.save()
        again = services.ensure_payout(order)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Payout.objects.count(), 1)
        self.assertEqual(again.commission_percent, Decimal('10.0'))

    def test_delivery_confirmation_creates_exactly_one_payout(self):
        order = make_order(self.seller, self.buyer, payment_status='escrow_held', order_status='out_for_delivery')
        Shipment.objects.create(order=order, delivery_otp='1234')
        self.client.login(username='buy', password='pw')
        for _ in range(3):
            self.client.post(reverse('orders:confirm_delivery', args=[order.order_id]), {'otp': '1234'})
        payouts = Payout.objects.filter(order=order)
        self.assertEqual(payouts.count(), 1)
        self.assertEqual(payouts.get().seller, self.seller)

    def test_order_deletion_keeps_the_financial_record(self):
        order = make_order(self.seller, self.buyer, title='Gone Book')
        payout = services.ensure_payout(order)
        order.delete()
        payout.refresh_from_db()
        self.assertIsNone(payout.order)
        self.assertEqual(payout.book_title, 'Gone Book')


@LOCMEM

def _name_verified(user):
    """Mark the seller's current UPI ID as name-checked, so tests that are not about the
    name check can still record payouts."""
    PayoutProfile.objects.filter(user=user).update(verified_upi=PayoutProfile.objects.get(user=user).upi_id, verified_name='Test Name')


class MarkPaidTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'b@x.com', 'pw', is_staff=True, is_superuser=True)
        self.s1 = User.objects.create_user('s1', 's1@x.com', 'pw')
        self.s2 = User.objects.create_user('s2', 's2@x.com', 'pw')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        services.set_upi_id(self.s1, 'one@ybl')
        services.set_upi_id(self.s2, 'two@ybl')
        _name_verified(self.s1)
        _name_verified(self.s2)
        self.p1 = services.ensure_payout(make_order(self.s1, self.buyer, price=500))
        self.p1b = services.ensure_payout(make_order(self.s1, self.buyer, price=300))
        self.p2 = services.ensure_payout(make_order(self.s2, self.buyer, price=200))

    def test_one_transfer_can_settle_several_orders_of_one_seller(self):
        paid = services.mark_paid([self.p1.pk, self.p1b.pk], utr='123456789012', actor=self.staff)
        self.assertEqual(len(paid), 2)
        for p in Payout.objects.filter(pk__in=[self.p1.pk, self.p1b.pk]):
            self.assertEqual((p.status, p.utr, p.paid_to_upi, p.paid_by), ('paid', '123456789012', 'one@ybl', self.staff))
            self.assertIsNotNone(p.paid_at)

    def test_rejections(self):
        with self.assertRaises(services.PayoutError):
            services.mark_paid([self.p1.pk], utr='bad utr!', actor=self.staff)
        with self.assertRaises(services.PayoutError):
            services.mark_paid([self.p1.pk], utr='', actor=self.staff)
        with self.assertRaises(services.PayoutError) as ctx:
            services.mark_paid([self.p1.pk, self.p2.pk], utr='123456789012', actor=self.staff)
        self.assertIn('one seller', str(ctx.exception).lower())
        with self.assertRaises(services.PayoutError):
            services.mark_paid([self.p1.pk, 99999], utr='123456789012', actor=self.staff)
        self.assertFalse(Payout.objects.filter(status='paid').exists())  # nothing half-applied

    def test_cannot_pay_twice_or_pay_held(self):
        services.mark_paid([self.p1.pk], utr='123456789012', actor=self.staff)
        with self.assertRaises(services.PayoutError):
            services.mark_paid([self.p1.pk], utr='999999999999', actor=self.staff)
        services.put_on_hold(self.p1b.pk, note='checking')
        with self.assertRaises(services.PayoutError):
            services.mark_paid([self.p1b.pk], utr='123456789012', actor=self.staff)

    def test_seller_without_upi_cannot_be_paid(self):
        s3 = User.objects.create_user('s3', 's3@x.com', 'pw')
        p3 = services.ensure_payout(make_order(s3, self.buyer))
        with self.assertRaises(services.PayoutError) as ctx:
            services.mark_paid([p3.pk], utr='123456789012', actor=self.staff)
        self.assertIn('UPI', str(ctx.exception))

    def test_same_utr_for_a_different_seller_is_refused(self):
        services.mark_paid([self.p1.pk], utr='123456789012', actor=self.staff)
        with self.assertRaises(services.PayoutError):
            services.mark_paid([self.p2.pk], utr='123456789012', actor=self.staff)

    def test_hold_release_cancel_rules(self):
        with self.assertRaises(services.PayoutError):
            services.put_on_hold(self.p1.pk, note='  ')
        services.put_on_hold(self.p1.pk, note='dispute')
        self.assertEqual(Payout.objects.get(pk=self.p1.pk).status, 'on_hold')
        services.release_hold(self.p1.pk)
        self.assertEqual(Payout.objects.get(pk=self.p1.pk).status, 'pending')
        with self.assertRaises(services.PayoutError):
            services.cancel(self.p1.pk, note='')
        services.cancel(self.p1.pk, note='duplicate')
        with self.assertRaises(services.PayoutError):
            services.release_hold(self.p1.pk)
        services.mark_paid([self.p1b.pk], utr='123456789012', actor=self.staff)
        with self.assertRaises(services.PayoutError):
            services.cancel(self.p1b.pk, note='too late')

    def test_refund_voids_unpaid_and_flags_paid(self):
        services.mark_paid([self.p1.pk], utr='123456789012', actor=self.staff)
        services.void_for_refund(self.p1.order)
        services.void_for_refund(self.p2.order)
        self.p1.refresh_from_db(); self.p2.refresh_from_db()
        self.assertEqual((self.p1.status, self.p1.refunded_after_payout), ('paid', True))
        self.assertEqual((self.p2.status, self.p2.refunded_after_payout), ('cancelled', False))


@LOCMEM
class PayoutViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'b@x.com', 'pw', is_staff=True, is_superuser=True)
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        self.other = User.objects.create_user('other', 'o@x.com', 'pw')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        services.set_upi_id(self.seller, 'sel@ybl')
        _name_verified(self.seller)
        self.payout = services.ensure_payout(make_order(self.seller, self.buyer, price=500, title='=HYPERLINK("http://evil")'))
        self.other_payout = services.ensure_payout(make_order(self.other, self.buyer, price=777, title='Other Seller Book'))

    def test_login_and_staff_gates(self):
        for name in ('payouts:mine', 'payouts:statement_pdf', 'payouts:staff_queue', 'payouts:staff_export'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        self.client.login(username='sel', password='pw')
        for name in ('payouts:staff_queue', 'payouts:staff_export'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        r = self.client.post(reverse('payouts:staff_mark_paid'), {'payout_ids': [self.payout.pk], 'utr': '123456789012'})
        self.assertEqual(r.status_code, 302)
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, 'pending')

    def test_seller_page_shows_only_own_payouts(self):
        self.client.login(username='sel', password='pw')
        html = self.client.get(reverse('payouts:mine')).content.decode()
        self.assertIn(self.payout.order_ref, html)
        self.assertNotIn('Other Seller Book', html)
        self.assertNotIn(self.other_payout.order_ref, html)

    def test_update_upi_validates_and_emails_on_change(self):
        cache.clear()
        self.client.login(username='sel', password='pw')
        self.client.post(reverse('payouts:update_upi'), {'upi_id': 'not a upi'})
        self.assertEqual(services.get_upi_id(self.seller), 'sel@ybl')
        self.assertEqual(len(mail.outbox), 0)
        self.client.post(reverse('payouts:update_upi'), {'upi_id': 'NEW@oksbi', 'current_password': 'pw'})
        self.assertEqual(services.get_upi_id(self.seller), 'new@oksbi')
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('new@oksbi', mail.outbox[0].body)  # masked in the email
        self.client.post(reverse('payouts:update_upi'), {'upi_id': 'new@oksbi'})
        self.assertEqual(len(mail.outbox), 1)  # unchanged -> no new alert

    def test_seller_statement_downloads(self):
        self.client.login(username='sel', password='pw')
        period = statements.period_choices()[0][0]
        pdf = self.client.get(reverse('payouts:statement_pdf'), {'period': period})
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        self.assertIn('attachment', pdf['Content-Disposition'])
        csv_resp = self.client.get(reverse('payouts:statement_csv'), {'period': period})
        body = csv_resp.content.decode()
        self.assertIn('Net payout', body)
        self.assertNotIn('Other Seller Book', body)
        self.assertIn("'=HYPERLINK", body)  # formula neutralised
        self.assertNotIn(',=HYPERLINK', body)

    def test_bad_or_missing_period_is_404(self):
        self.client.login(username='sel', password='pw')
        for params in ({}, {'period': '2026-13'}, {'period': 'x'}):
            self.assertEqual(self.client.get(reverse('payouts:statement_pdf'), params).status_code, 404)

    def test_staff_marks_paid_with_audit_and_notification(self):
        self.client.login(username='boss', password='pw')
        self.assertEqual(self.client.get(reverse('payouts:staff_queue')).status_code, 200)
        self.assertEqual(self.client.get(reverse('payouts:staff_detail', args=[self.payout.pk])).status_code, 200)
        self.client.post(reverse('payouts:staff_mark_paid'), {'payout_ids': [self.payout.pk], 'utr': '123456789012'})
        self.payout.refresh_from_db()
        self.assertEqual((self.payout.status, self.payout.utr), ('paid', '123456789012'))
        log = AuditLog.objects.get(action='mark_payout_paid')
        self.assertEqual(log.actor, self.staff)
        self.assertIn('123456789012', log.note)
        self.assertTrue(Notification.objects.filter(user=self.seller, title__startswith='Payout of').exists())

    def test_staff_hold_via_view_requires_reason(self):
        self.client.login(username='boss', password='pw')
        url = reverse('payouts:staff_action', args=[self.payout.pk])
        self.client.post(url, {'action': 'hold', 'note': ''})
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, 'pending')
        self.client.post(url, {'action': 'hold', 'note': 'dispute'})
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, 'on_hold')
        self.assertTrue(AuditLog.objects.filter(action='hold_payout').exists())

    def test_staff_export_neutralises_formulas(self):
        self.client.login(username='boss', password='pw')
        body = self.client.get(reverse('payouts:staff_export'), {'status': 'all'}).content.decode()
        self.assertIn('Other Seller Book', body)
        self.assertIn("'=HYPERLINK", body)


class PeriodTests(TestCase):
    def test_parsing(self):
        m = statements.parse_period('2026-12')
        self.assertEqual((m.label, m.start.month, m.end.year, m.end.month), ('December 2026', 12, 2027, 1))
        fy = statements.parse_period('fy-2026')
        self.assertEqual((fy.label, fy.start.month, fy.end.month, fy.end.year), ('FY 2026-27', 4, 4, 2027))
        for bad in ('', '2026-00', '2026-13', 'fy-26', 'abc'):
            with self.assertRaises(statements.InvalidPeriod):
                statements.parse_period(bad)

    def test_months_are_cut_at_midnight_ist_not_utc(self):
        seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        p = services.ensure_payout(make_order(seller, buyer))
        # 19:00 UTC on 31 Aug is 00:30 IST on 1 Sep.
        Payout.objects.filter(pk=p.pk).update(released_at=datetime(2026, 8, 31, 19, 0, tzinfo=dt_tz.utc))
        self.assertEqual(len(statements.seller_payouts(seller, statements.parse_period('2026-09'))), 1)
        self.assertEqual(len(statements.seller_payouts(seller, statements.parse_period('2026-08'))), 0)

    def test_cancelled_payouts_are_left_off_statements(self):
        seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        p = services.ensure_payout(make_order(seller, buyer))
        services.cancel(p.pk, note='refunded')
        period = statements.period_choices()[0]
        self.assertEqual(statements.seller_payouts(seller, statements.parse_period(period[0])), [])


@LOCMEM
class PartnerStatementTests(TestCase):
    def setUp(self):
        self.shop_user = User.objects.create_user('shop', 'shop@x.com', 'pw')
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        self.partner = _make_partner(self.shop_user, status='approved', gstin='09ABCDE1234F1Z5')
        b = BuybackRequest.objects.create(
            seller=self.seller, partner=self.partner, title='+cmd|calc', pickup_address='5 Street',
            pickup_city='Lucknow', status='paid', offered_price=150, final_price=150,
        )
        PartnerCommission.objects.create(partner=self.partner, buyback_request=b, amount=Decimal('15.00'))

    def test_partner_can_download_both_formats(self):
        self.client.login(username='shop', password='pw')
        period = statements.period_choices()[0][0]
        pdf = self.client.get(reverse('payouts:partner_statement_pdf'), {'period': period})
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        body = self.client.get(reverse('payouts:partner_statement_csv'), {'period': period}).content.decode()
        self.assertIn('15.00', body)
        self.assertIn("'+cmd|calc", body)

    def test_non_partner_is_turned_away(self):
        self.client.login(username='sel', password='pw')
        r = self.client.get(reverse('payouts:partner_statement_pdf'), {'period': statements.period_choices()[0][0]})
        self.assertRedirects(r, reverse('partners:apply'), fetch_redirect_response=False)

    def test_earnings_page_offers_the_download(self):
        self.client.login(username='shop', password='pw')
        html = self.client.get(reverse('partners:earnings')).content.decode()
        self.assertIn(reverse('payouts:partner_statement_pdf'), html)


class BackfillCommandTests(TestCase):
    def test_backfill(self):
        from django.core.management import call_command
        seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        make_order(seller, buyer, price=400)
        make_order(seller, buyer, price=100, payment_status='refunded')  # not released -> skipped
        call_command('backfill_payouts', '--mark-paid', verbosity=0)
        call_command('backfill_payouts', verbosity=0)  # second run is a no-op
        self.assertEqual(Payout.objects.count(), 1)
        p = Payout.objects.get()
        self.assertEqual(p.status, 'paid')
        self.assertEqual(p.utr, '')


class NameMatchTests(TestCase):
    def test_matches_across_case_order_titles_and_initials(self):
        for a, b in [
            ('Rajesh Gaur', 'RAJESH KUMAR GAUR'),
            ('R K Gaur', 'Rajesh Kumar Gaur'),
            ('Gaur Rajesh', 'RAJESH GAUR'),
            ('Mr. Rajesh Gaur', 'rajesh gaur'),
            ('Priya Sharma', 'PRIYA SHARMAA'),
        ]:
            self.assertTrue(names_match(a, b), (a, b))

    def test_different_people_do_not_match(self):
        for a, b in [
            ('Rajesh Gaur', 'AMIT VERMA'),
            ('Rajesh Gaur', 'RAJESH VERMA'),
            ('R K', 'RAVI KUMAR'),            # initials alone are not enough
            ('', 'RAJESH GAUR'),
            ('Rajesh Gaur', ''),
        ]:
            self.assertFalse(names_match(a, b), (a, b))


class NameCheckServiceTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'b@x.com', 'pw', is_staff=True, is_superuser=True)
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw', first_name='Rajesh', last_name='Gaur')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        services.set_upi_id(self.seller, 'raj@ybl')
        self.payout = services.ensure_payout(make_order(self.seller, self.buyer, price=500))

    def test_first_payout_is_blocked_until_the_name_is_checked(self):
        self.assertTrue(services.needs_name_check(self.seller))
        with self.assertRaisesMessage(services.PayoutError, 'Check the UPI name'):
            services.mark_paid([self.payout.pk], utr='123456789012', actor=self.staff)
        services.record_name_check(self.seller, 'RAJESH KUMAR GAUR', actor=self.staff)
        self.assertFalse(services.needs_name_check(self.seller))
        services.mark_paid([self.payout.pk], utr='123456789012', actor=self.staff)

    def test_mismatch_is_refused_unless_overridden_with_a_note(self):
        with self.assertRaises(services.NameMismatch):
            services.record_name_check(self.seller, 'AMIT VERMA', actor=self.staff)
        with self.assertRaises(services.PayoutError):   # override but no note
            services.record_name_check(self.seller, 'AMIT VERMA', actor=self.staff, override=True)
        profile, matched = services.record_name_check(
            self.seller, 'AMIT VERMA', actor=self.staff, override=True, note='Seller said it is his brother\'s account')
        self.assertFalse(matched)
        self.assertTrue(profile.name_check_override)
        self.assertTrue(profile.is_name_verified)

    def test_changing_the_upi_id_cancels_the_check(self):
        services.record_name_check(self.seller, 'Rajesh Gaur', actor=self.staff)
        self.assertFalse(services.needs_name_check(self.seller))
        services.set_upi_id(self.seller, 'other@okaxis')
        self.assertTrue(services.needs_name_check(self.seller))

    def test_later_payouts_to_a_paid_upi_id_need_no_new_check(self):
        services.record_name_check(self.seller, 'Rajesh Gaur', actor=self.staff)
        services.mark_paid([self.payout.pk], utr='123456789012', actor=self.staff)
        PayoutProfile.objects.filter(user=self.seller).update(verified_upi='')   # even with no stored check
        self.assertFalse(services.needs_name_check(self.seller))

    def test_seller_without_a_profile_name_needs_an_override(self):
        nameless = User.objects.create_user('nameless', 'n@x.com', 'pw')
        services.set_upi_id(nameless, 'nameless@ybl')
        with self.assertRaises(services.NameMismatch):
            services.record_name_check(nameless, 'SOME PERSON', actor=self.staff)

    def test_garbage_name_is_rejected(self):
        for bad in ('', ' ', '1', '12345'):
            with self.assertRaises(services.PayoutError):
                services.record_name_check(self.seller, bad, actor=self.staff)


class NameCheckViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'b@x.com', 'pw', is_staff=True, is_superuser=True)
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw', first_name='Rajesh', last_name='Gaur')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        services.set_upi_id(self.seller, 'raj@ybl')
        self.payout = services.ensure_payout(make_order(self.seller, self.buyer, price=500))
        self.url = reverse('payouts:staff_check_upi_name', args=[self.payout.pk])

    def test_staff_only(self):
        self.assertEqual(self.client.post(self.url, {'upi_name': 'Rajesh Gaur'}).status_code, 302)
        self.client.login(username='sel', password='pw')
        self.client.post(self.url, {'upi_name': 'Rajesh Gaur'})
        self.assertTrue(services.needs_name_check(self.seller))   # the seller can't verify themselves

    def test_match_is_recorded_and_audited(self):
        self.client.login(username='boss', password='pw')
        self.client.post(self.url, {'upi_name': 'RAJESH KUMAR GAUR'})
        self.assertFalse(services.needs_name_check(self.seller))
        log = AuditLog.objects.filter(action='check_upi_name').latest('pk')
        self.assertIn('matched', log.note)

    def test_mismatch_shows_error_and_accept_anyway_is_flagged(self):
        self.client.login(username='boss', password='pw')
        self.client.post(self.url, {'upi_name': 'AMIT VERMA'})
        self.assertTrue(services.needs_name_check(self.seller))
        self.client.post(self.url, {'upi_name': 'AMIT VERMA', 'override': 'on', 'note': 'brother account'})
        self.assertFalse(services.needs_name_check(self.seller))
        self.assertIn('ACCEPTED DESPITE MISMATCH', AuditLog.objects.filter(action='check_upi_name').latest('pk').note)

    def test_detail_page_renders_once_and_shows_the_check_card(self):
        self.client.login(username='boss', password='pw')
        r = self.client.get(reverse('payouts:staff_detail', args=[self.payout.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Needed before first payout')
        self.assertContains(r, 'Rajesh Gaur')
        self.assertEqual(r.content.decode().count('Hold or cancel'), 1)   # the template was once repeated thousands of times
        self.assertNotContains(r, 'Aadhaar')

    def test_seller_page_explains_the_check(self):
        self.client.login(username='sel', password='pw')
        r = self.client.get(reverse('payouts:mine'))
        self.assertContains(r, 'matches your account name')


@LOCMEM
class UpiChangeHoldTests(TestCase):
    """After a seller changes their UPI ID, no payout is sent for 72 hours (account-takeover defence)."""

    def setUp(self):
        from datetime import timedelta
        cache.clear()  # the UPI-change view is rate limited per user, and the cache outlives a test
        self.addCleanup(cache.clear)
        self.timedelta = timedelta
        self.staff = User.objects.create_user('boss', 'b@x.com', 'pw', is_staff=True, is_superuser=True)
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        services.set_upi_id(self.seller, 'old@ybl')
        self.payout = services.ensure_payout(make_order(self.seller, self.buyer, price=500))

    def _change_via_view(self, upi='new@oksbi', password='pw'):
        self.client.login(username='sel', password='pw')
        return self.client.post(reverse('payouts:update_upi'), {'upi_id': upi, 'current_password': password})

    def test_changing_upi_needs_the_account_password(self):
        self._change_via_view(password='wrong')
        self.assertEqual(services.get_upi_id(self.seller), 'old@ybl')
        self._change_via_view(password='')
        self.assertEqual(services.get_upi_id(self.seller), 'old@ybl')
        self._change_via_view(password='pw')
        self.assertEqual(services.get_upi_id(self.seller), 'new@oksbi')

    def test_change_is_audited_and_starts_the_hold(self):
        self._change_via_view()
        profile = PayoutProfile.objects.get(user=self.seller)
        self.assertIsNotNone(profile.upi_changed_at)
        self.assertTrue(AuditLog.objects.filter(action='seller_change_upi').exists())
        self.assertIsNotNone(services.upi_hold_remaining(profile))

    def test_payout_is_blocked_during_the_hold_even_after_name_check(self):
        self._change_via_view()
        _name_verified(self.seller)
        with self.assertRaises(services.PayoutError) as ctx:
            services.mark_paid([self.payout.pk], utr='123456789012', actor=self.staff)
        self.assertIn('security hold', str(ctx.exception))
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, 'pending')

    def test_payout_works_once_the_hold_has_passed(self):
        self._change_via_view()
        _name_verified(self.seller)
        PayoutProfile.objects.filter(user=self.seller).update(upi_changed_at=timezone.now() - self.timedelta(hours=73))
        services.mark_paid([self.payout.pk], utr='123456789012', actor=self.staff)
        self.payout.refresh_from_db()
        self.assertEqual((self.payout.status, self.payout.paid_to_upi), ('paid', 'new@oksbi'))

    def test_saving_the_same_upi_does_not_restart_the_hold(self):
        services.set_upi_id(self.seller, 'old@ybl', start_hold=True)
        first = PayoutProfile.objects.get(user=self.seller).upi_changed_at
        services.set_upi_id(self.seller, 'old@ybl', start_hold=True)
        self.assertEqual(PayoutProfile.objects.get(user=self.seller).upi_changed_at, first)

    def test_override_needs_a_reason_unblocks_and_is_audited(self):
        self._change_via_view()
        _name_verified(self.seller)
        self.client.logout()
        self.client.login(username='boss', password='pw')
        url = reverse('payouts:staff_override_upi_hold', args=[self.payout.pk])
        self.client.post(url, {'reason': 'short'})
        self.assertIsNotNone(services.upi_hold_remaining(PayoutProfile.objects.get(user=self.seller)))
        self.client.post(url, {'reason': 'Called seller on the phone number saved at signup; confirmed.'})
        self.assertIsNone(services.upi_hold_remaining(PayoutProfile.objects.get(user=self.seller)))
        self.assertTrue(AuditLog.objects.filter(action='override_upi_hold').exists())
        services.mark_paid([self.payout.pk], utr='123456789012', actor=self.staff)

    def test_override_does_not_carry_over_to_the_next_upi_change(self):
        self._change_via_view()
        services.override_upi_hold(self.seller, actor=self.staff, reason='Confirmed with the seller by phone.')
        self._change_via_view(upi='third@okaxis')
        self.assertIsNotNone(services.upi_hold_remaining(PayoutProfile.objects.get(user=self.seller)))

    def test_staff_pages_show_the_hold(self):
        self._change_via_view()
        self.client.logout()
        self.client.login(username='boss', password='pw')
        self.assertContains(self.client.get(reverse('payouts:staff_detail', args=[self.payout.pk])), 'Override hold')
        self.assertContains(self.client.get(reverse('payouts:staff_queue')), 'UPI hold')
