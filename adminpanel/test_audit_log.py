from django.contrib import admin as django_admin
from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from adminpanel.models import AuditLog

from .tests import make_book

PW = 'Str0ng!Pass#123'
UA = 'Mozilla/5.0 (TestBrowser) ' + 'x' * 400


class AuditRecordTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('staff1', 's@x.com', PW, is_staff=True, is_superuser=True)

    def test_record_without_request_still_works_and_new_fields_are_empty(self):
        e = AuditLog.record(actor=self.staff, action='approve_book', target_repr='Book')
        self.assertIsNone(e.ip_address)
        self.assertEqual((e.user_agent, e.reason), ('', ''))
        self.assertIsNone(e.old_value)
        self.assertIsNone(e.new_value)

    def test_request_context_old_new_and_reason_are_captured(self):
        req = RequestFactory().post('/x/', HTTP_USER_AGENT=UA, REMOTE_ADDR='203.0.113.9')
        e = AuditLog.record(actor=self.staff, action='remove_book', target_repr='Book', request=req,
                            old={'status': 'available'}, new={'status': 'removed'}, reason='Counterfeit')
        e.refresh_from_db()
        self.assertEqual(e.ip_address, '203.0.113.9')
        self.assertEqual(len(e.user_agent), 255)  # clipped, not rejected
        self.assertEqual((e.old_value, e.new_value, e.reason), ({'status': 'available'}, {'status': 'removed'}, 'Counterfeit'))

    def test_spoofed_forwarded_for_is_ignored_without_trusted_proxies(self):
        req = RequestFactory().post('/x/', REMOTE_ADDR='203.0.113.9', HTTP_X_FORWARDED_FOR='1.2.3.4')
        self.assertEqual(AuditLog.record(actor=self.staff, action='approve_book', target_repr='b', request=req).ip_address, '203.0.113.9')

    def test_unparseable_ip_is_stored_as_null_not_an_error(self):
        req = RequestFactory().post('/x/', REMOTE_ADDR='not-an-ip')
        self.assertIsNone(AuditLog.record(actor=self.staff, action='approve_book', target_repr='b', request=req).ip_address)

    def test_decimals_and_dates_are_json_safe_and_huge_snapshots_are_truncated(self):
        from datetime import date
        from decimal import Decimal
        e = AuditLog.record(actor=self.staff, action='refund_issued', target_repr='o',
                            new={'amount': Decimal('199.50'), 'on': date(2026, 10, 5)})
        e.refresh_from_db()
        self.assertEqual(e.new_value, {'amount': '199.50', 'on': '2026-10-05'})
        big = AuditLog.record(actor=self.staff, action='refund_issued', target_repr='o', old={'blob': 'x' * 10000})
        self.assertEqual(big.old_value, {'truncated': True})


class AppendOnlyTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('staff2', 's2@x.com', PW, is_staff=True, is_superuser=True)
        self.entry = AuditLog.record(actor=self.staff, action='approve_book', target_repr='Book', new={'status': 'available'})

    def test_existing_rows_cannot_be_saved_again(self):
        self.entry.note = 'rewritten history'
        with self.assertRaises(PermissionDenied):
            self.entry.save()
        self.assertEqual(AuditLog.objects.get(pk=self.entry.pk).note, '')

    def test_rows_cannot_be_deleted_one_by_one_or_in_bulk(self):
        with self.assertRaises(PermissionDenied):
            self.entry.delete()
        with self.assertRaises(PermissionDenied):
            AuditLog.objects.all().delete()
        with self.assertRaises(PermissionDenied):
            AuditLog.objects.filter(pk=self.entry.pk).update(note='x')
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_deleting_the_staff_user_keeps_the_entry(self):
        self.staff.delete()
        kept = AuditLog.objects.get(pk=self.entry.pk)
        self.assertIsNone(kept.actor)
        self.assertEqual(kept.target_repr, 'Book')

    def test_django_admin_offers_no_add_change_or_delete(self):
        model_admin = site._registry[AuditLog]
        request = RequestFactory().get('/admin/')
        request.user = self.staff
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.entry))
        self.assertFalse(model_admin.has_delete_permission(request, self.entry))


class StaffActionsRecordContextTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'b@x.com', PW, is_staff=True, is_superuser=True)
        self.seller = User.objects.create_user('seller9', 'sl@x.com', PW)
        self.client.force_login(self.staff)

    def _post(self, url, data=None):
        return self.client.post(url, data or {}, HTTP_USER_AGENT='AuditTestBrowser/1.0', REMOTE_ADDR='198.51.100.7')

    def test_approving_a_listing_records_who_where_and_what_changed(self):
        book = make_book(self.seller)
        self._post(reverse('adminpanel:approve_book', args=[book.pk]))
        e = AuditLog.objects.get(action='approve_book')
        self.assertEqual((e.actor, e.ip_address, e.user_agent), (self.staff, '198.51.100.7', 'AuditTestBrowser/1.0'))
        self.assertEqual((e.old_value, e.new_value), ({'status': 'under_review'}, {'status': 'available'}))

    def test_removing_a_listing_records_the_reason(self):
        book = make_book(self.seller, status='available')
        self._post(reverse('adminpanel:remove_book', args=[book.pk]), {'reason': 'Counterfeit copy'})
        e = AuditLog.objects.get(action='remove_book')
        self.assertEqual(e.reason, 'Counterfeit copy')
        self.assertEqual((e.old_value, e.new_value), ({'status': 'available'}, {'status': 'removed'}))

    def test_clearing_a_duplicate_flag_records_the_old_flag(self):
        self.seller.is_duplicate_flagged = True
        self.seller.duplicate_flag_reason = 'Same phone as another account'
        self.seller.save()
        self._post(reverse('adminpanel:clear_duplicate_flag', args=[self.seller.pk]), {'reason': 'Family members'})
        e = AuditLog.objects.get(action='clear_duplicate_flag')
        self.assertEqual(e.old_value, {'is_duplicate_flagged': True, 'reason': 'Same phone as another account'})
        self.assertEqual((e.new_value, e.reason), ({'is_duplicate_flagged': False}, 'Family members'))
