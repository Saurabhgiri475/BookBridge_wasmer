from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from books.models import Book, Category, ListingReport
from notifications.models import Notification
from orders.models import Order, ReturnRequest

from .models import AuditLog
from .views import PAGE_SIZE


def make_book(seller, **kwargs):
    category, _ = Category.objects.get_or_create(name='Test Category')
    defaults = {'title': 'Some Book', 'price': 199, 'status': 'under_review'}
    defaults.update(kwargs)
    return Book.objects.create(seller=seller, category=category, **defaults)


class StaffRequiredTests(TestCase):
    """The dashboard and every action behind it must bounce non-staff
    through the site's own login page (LOGIN_URL = 'accounts:login'),
    not Django admin's - see adminpanel/views.py:staff_required."""

    def setUp(self):
        self.dashboard_url = reverse('adminpanel:dashboard')

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_non_staff_user_redirected_to_login(self):
        User.objects.create_user(username='buyer1', password='pw')
        self.client.login(username='buyer1', password='pw')
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_staff_user_can_access(self):
        User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)


class DashboardStatsTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw')

    def test_stats_reflect_actual_counts(self):
        make_book(self.seller, title='Under review 1', status='under_review')
        make_book(self.seller, title='Available 1', status='available')
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(response.context['stats']['pending_review_count'], 1)
        self.assertEqual(response.context['stats']['books_listed'], 1)

    def test_empty_states_render(self):
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(response, "Nothing waiting on review")
        self.assertContains(response, "No accounts currently flagged")
        self.assertContains(response, "No return requests waiting")
        self.assertContains(response, "No open reports from buyers")


class DashboardSearchTests(TestCase):
    """?review_q=/?live_flagged_q=/?flagged_q= narrow each list's table by
    book title or seller/account username, without changing the stat
    badges (which always reflect true unfiltered totals) - see
    adminpanel/views.py:dashboard."""

    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')

    def test_review_q_filters_by_book_title(self):
        make_book(self.alice, title='Organic Chemistry Vol 1', status='under_review')
        make_book(self.bob, title='Intro to Physics', status='under_review')
        response = self.client.get(reverse('adminpanel:dashboard'), {'review_q': 'chemistry'})
        self.assertContains(response, 'Organic Chemistry Vol 1')
        self.assertNotContains(response, 'Intro to Physics')

    def test_review_q_filters_by_seller_username(self):
        make_book(self.alice, title='Organic Chemistry Vol 1', status='under_review')
        make_book(self.bob, title='Intro to Physics', status='under_review')
        response = self.client.get(reverse('adminpanel:dashboard'), {'review_q': 'alice'})
        self.assertContains(response, 'Organic Chemistry Vol 1')
        self.assertNotContains(response, 'Intro to Physics')

    def test_review_q_does_not_change_stat_badge(self):
        make_book(self.alice, title='Organic Chemistry Vol 1', status='under_review')
        make_book(self.bob, title='Intro to Physics', status='under_review')
        response = self.client.get(reverse('adminpanel:dashboard'), {'review_q': 'chemistry'})
        self.assertEqual(response.context['stats']['pending_review_count'], 2)

    def test_review_q_no_match_shows_no_match_message(self):
        make_book(self.alice, title='Organic Chemistry Vol 1', status='under_review')
        response = self.client.get(reverse('adminpanel:dashboard'), {'review_q': 'nonexistent'})
        self.assertContains(response, 'No listings under review match')

    def test_live_flagged_q_filters_by_book_title(self):
        make_book(self.alice, title='Flagged Live Chem', status='available', fraud_score=30)
        make_book(self.bob, title='Flagged Live Physics', status='available', fraud_score=30)
        response = self.client.get(reverse('adminpanel:dashboard'), {'live_flagged_q': 'chem'})
        self.assertContains(response, 'Flagged Live Chem')
        self.assertNotContains(response, 'Flagged Live Physics')

    def test_flagged_q_filters_duplicate_accounts_by_username(self):
        self.alice.is_duplicate_flagged = True
        self.alice.duplicate_flag_reason = 'dupe of bob (same phone)'
        self.alice.save(update_fields=['is_duplicate_flagged', 'duplicate_flag_reason'])
        self.bob.is_duplicate_flagged = True
        self.bob.duplicate_flag_reason = 'dupe of alice (same phone)'
        self.bob.save(update_fields=['is_duplicate_flagged', 'duplicate_flag_reason'])
        response = self.client.get(reverse('adminpanel:dashboard'), {'flagged_q': 'ali'})
        self.assertContains(response, 'alice')
        self.assertNotContains(response, '>bob<')


class ApproveBookTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw', email='seller1@example.com')
        self.book = make_book(self.seller, title='Flagged Book', status='under_review')

    def test_get_not_allowed(self):
        response = self.client.get(reverse('adminpanel:approve_book', args=[self.book.pk]))
        self.assertEqual(response.status_code, 405)

    def test_approve_publishes_book(self):
        response = self.client.post(reverse('adminpanel:approve_book', args=[self.book.pk]))
        self.assertRedirects(response, reverse('adminpanel:dashboard'))
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'available')

    def test_approve_notifies_seller(self):
        self.client.post(reverse('adminpanel:approve_book', args=[self.book.pk]))
        notif = Notification.objects.filter(user=self.seller).first()
        self.assertIsNotNone(notif)
        self.assertIn(self.book.title, notif.title)

    def test_approve_writes_audit_log(self):
        self.client.post(reverse('adminpanel:approve_book', args=[self.book.pk]))
        log = AuditLog.objects.get(action='approve_book')
        self.assertEqual(log.actor, self.staff)
        self.assertEqual(log.target_repr, self.book.title)
        self.assertEqual(log.target_id, self.book.pk)

    def test_non_staff_cannot_approve(self):
        self.client.logout()
        User.objects.create_user(username='rando', password='pw')
        self.client.login(username='rando', password='pw')
        self.client.post(reverse('adminpanel:approve_book', args=[self.book.pk]))
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'under_review')


class RemoveBookTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw', email='seller1@example.com')
        self.book = make_book(self.seller, title='Bad Book', status='under_review')

    def test_remove_takes_book_down(self):
        response = self.client.post(reverse('adminpanel:remove_book', args=[self.book.pk]))
        self.assertRedirects(response, reverse('adminpanel:dashboard'))
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, 'removed')

    def test_remove_notifies_seller_and_logs(self):
        self.client.post(reverse('adminpanel:remove_book', args=[self.book.pk]))
        self.assertTrue(Notification.objects.filter(user=self.seller).exists())
        log = AuditLog.objects.get(action='remove_book')
        self.assertEqual(log.target_repr, self.book.title)

    def test_remove_with_staff_reason_saved_on_log(self):
        self.client.post(reverse('adminpanel:remove_book', args=[self.book.pk]), {'reason': 'Confirmed stolen cover photo'})
        log = AuditLog.objects.get(action='remove_book')
        self.assertEqual(log.note, 'Confirmed stolen cover photo')

    def test_remove_without_reason_leaves_log_note_blank(self):
        self.client.post(reverse('adminpanel:remove_book', args=[self.book.pk]))
        log = AuditLog.objects.get(action='remove_book')
        self.assertEqual(log.note, '')


class ClearDuplicateFlagTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.flagged = User.objects.create_user(
            username='dupe1', password='pw',
            is_duplicate_flagged=True, duplicate_flag_reason='dupe2 (same phone)',
        )

    def test_clears_flag_fields(self):
        self.client.post(reverse('adminpanel:clear_duplicate_flag', args=[self.flagged.pk]))
        self.flagged.refresh_from_db()
        self.assertFalse(self.flagged.is_duplicate_flagged)
        self.assertEqual(self.flagged.duplicate_flag_reason, '')

    def test_audit_log_keeps_original_reason(self):
        self.client.post(reverse('adminpanel:clear_duplicate_flag', args=[self.flagged.pk]))
        log = AuditLog.objects.get(action='clear_duplicate_flag')
        self.assertEqual(log.target_repr, 'dupe1')
        self.assertEqual(log.note, 'dupe2 (same phone)')

    def test_audit_log_appends_staff_reason_to_original(self):
        self.client.post(
            reverse('adminpanel:clear_duplicate_flag', args=[self.flagged.pk]),
            {'reason': 'Confirmed siblings sharing a household number'},
        )
        log = AuditLog.objects.get(action='clear_duplicate_flag')
        self.assertIn('dupe2 (same phone)', log.note)
        self.assertIn('Confirmed siblings sharing a household number', log.note)


class FlaggedAccountLinkedMatchTests(TestCase):
    """The "Possible Match" column re-derives the actual matching account
    live via accounts.fraud_detection.find_linked_accounts, rather than
    just showing the free-text duplicate_flag_reason - see
    adminpanel/views.py:dashboard."""

    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')

    def test_matching_phone_account_shown_as_link(self):
        User.objects.create_user(username='original_seller', password='pw', phone='9998887777')
        User.objects.create_user(
            username='dupe_account', password='pw', phone='9998887777',
            is_duplicate_flagged=True, duplicate_flag_reason='original_seller (same phone number)',
        )
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(response, 'original_seller')
        self.assertContains(response, reverse('accounts:profile_view', args=['original_seller']))

    def test_no_current_match_shows_fallback_message(self):
        User.objects.create_user(
            username='dupe_account', password='pw',
            is_duplicate_flagged=True, duplicate_flag_reason='someone (same phone number)',
        )
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(response, 'No longer matches')


class DashboardPaginationTests(TestCase):
    """books_under_review/flagged_users/pending_returns each paginate
    independently (?review_page=/?flagged_page=/?returns_page=) - see
    adminpanel/views.py."""

    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw')
        for i in range(PAGE_SIZE + 5):
            make_book(self.seller, title=f'Book {i}', status='under_review')

    def test_first_page_capped_at_page_size(self):
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(len(response.context['books_under_review']), PAGE_SIZE)
        # stat count still reflects the true total, not just this page
        self.assertEqual(response.context['stats']['pending_review_count'], PAGE_SIZE + 5)

    def test_second_page_has_remainder(self):
        response = self.client.get(reverse('adminpanel:dashboard'), {'review_page': 2})
        self.assertEqual(len(response.context['books_under_review']), 5)


class FlaggedLiveBooksTests(TestCase):
    """adminpanel:clear_fraud_flag - the WARN_THRESHOLD-but-not-held case:
    a book that's already 'available' but still carries a fraud_score/
    fraud_flags worth a second look. See adminpanel/views.py."""

    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw', email='seller1@example.com')
        self.book = make_book(
            self.seller, title='Flagged But Live', status='available',
            fraud_score=30, fraud_flags='[{"check": "spam", "severity": "medium", "message": "test"}]',
        )

    def test_flagged_live_book_appears_on_dashboard(self):
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(response.context['stats']['flagged_live_count'], 1)
        self.assertContains(response, 'Flagged But Live')

    def test_unflagged_available_book_does_not_appear(self):
        make_book(self.seller, title='Clean Book', status='available', fraud_score=0)
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(response.context['stats']['flagged_live_count'], 1)

    def test_under_review_book_does_not_double_count(self):
        make_book(self.seller, title='Held Book', status='under_review', fraud_score=80)
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(response.context['stats']['flagged_live_count'], 1)
        self.assertEqual(response.context['stats']['pending_review_count'], 1)

    def test_get_not_allowed(self):
        response = self.client.get(reverse('adminpanel:clear_fraud_flag', args=[self.book.pk]))
        self.assertEqual(response.status_code, 405)

    def test_clear_fraud_flag_zeroes_fields_keeps_live(self):
        response = self.client.post(reverse('adminpanel:clear_fraud_flag', args=[self.book.pk]))
        self.assertRedirects(response, reverse('adminpanel:dashboard'))
        self.book.refresh_from_db()
        self.assertEqual(self.book.fraud_score, 0)
        self.assertEqual(self.book.fraud_flags, '')
        self.assertEqual(self.book.status, 'available')

    def test_clear_fraud_flag_writes_audit_log(self):
        self.client.post(reverse('adminpanel:clear_fraud_flag', args=[self.book.pk]))
        log = AuditLog.objects.get(action='clear_fraud_flag')
        self.assertEqual(log.actor, self.staff)
        self.assertEqual(log.target_repr, self.book.title)
        self.assertIn('spam', log.note)

    def test_clear_fraud_flag_appends_staff_reason(self):
        self.client.post(
            reverse('adminpanel:clear_fraud_flag', args=[self.book.pk]),
            {'reason': 'Verified with seller, listing is legitimate'},
        )
        log = AuditLog.objects.get(action='clear_fraud_flag')
        self.assertIn('spam', log.note)
        self.assertIn('Verified with seller, listing is legitimate', log.note)

    def test_non_staff_cannot_clear_fraud_flag(self):
        self.client.logout()
        User.objects.create_user(username='rando', password='pw')
        self.client.login(username='rando', password='pw')
        self.client.post(reverse('adminpanel:clear_fraud_flag', args=[self.book.pk]))
        self.book.refresh_from_db()
        self.assertEqual(self.book.fraud_score, 30)

    def test_cannot_clear_fraud_flag_on_non_available_book(self):
        self.book.status = 'under_review'
        self.book.save(update_fields=['status'])
        response = self.client.post(reverse('adminpanel:clear_fraud_flag', args=[self.book.pk]))
        self.assertEqual(response.status_code, 404)


class DismissReportTests(TestCase):
    """adminpanel:dismiss_report - the "not actionable" outcome for a
    buyer-filed ListingReport, as opposed to using the listing's own
    Approve/Remove buttons. See adminpanel/views.py."""

    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.reporter = User.objects.create_user(username='reporter1', password='pw')
        self.book = make_book(self.seller, title='Reported Book', status='available')
        self.report = ListingReport.objects.create(book=self.book, reporter=self.reporter, reason='fake')

    def test_get_not_allowed(self):
        response = self.client.get(reverse('adminpanel:dismiss_report', args=[self.report.pk]))
        self.assertEqual(response.status_code, 405)

    def test_dismiss_marks_report_dismissed_without_touching_book(self):
        response = self.client.post(reverse('adminpanel:dismiss_report', args=[self.report.pk]))
        self.assertRedirects(response, reverse('adminpanel:dashboard'))
        self.report.refresh_from_db()
        self.book.refresh_from_db()
        self.assertEqual(self.report.status, 'dismissed')
        self.assertEqual(self.book.status, 'available')

    def test_dismiss_writes_audit_log(self):
        self.client.post(reverse('adminpanel:dismiss_report', args=[self.report.pk]))
        log = AuditLog.objects.get(action='dismiss_report')
        self.assertEqual(log.actor, self.staff)
        self.assertEqual(log.target_repr, self.book.title)

    def test_cannot_dismiss_already_resolved_report(self):
        self.report.status = 'dismissed'
        self.report.save(update_fields=['status'])
        response = self.client.post(reverse('adminpanel:dismiss_report', args=[self.report.pk]))
        self.assertEqual(response.status_code, 404)

    def test_non_staff_cannot_dismiss(self):
        self.client.logout()
        User.objects.create_user(username='rando', password='pw')
        self.client.login(username='rando', password='pw')
        self.client.post(reverse('adminpanel:dismiss_report', args=[self.report.pk]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'pending')


class ReportedListingsDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw')
        self.reporter = User.objects.create_user(username='reporter1', password='pw')
        self.book = make_book(self.seller, title='Reported Book', status='available')

    def test_pending_report_appears_on_dashboard(self):
        ListingReport.objects.create(book=self.book, reporter=self.reporter, reason='scam')
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(response.context['stats']['pending_report_count'], 1)
        self.assertContains(response, 'Reported Book')

    def test_dismissed_report_does_not_count_as_pending(self):
        ListingReport.objects.create(
            book=self.book, reporter=self.reporter, reason='scam', status='dismissed',
        )
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(response.context['stats']['pending_report_count'], 0)

    def test_removing_reported_book_marks_pending_reports_actioned(self):
        report = ListingReport.objects.create(book=self.book, reporter=self.reporter, reason='fake')
        self.client.post(reverse('adminpanel:remove_book', args=[self.book.pk]))
        report.refresh_from_db()
        self.assertEqual(report.status, 'actioned')


class RecentActivityTests(TestCase):
    """The dashboard's read-back of AuditLog - see
    adminpanel/views.py:RECENT_ACTIVITY_LIMIT and AuditLog's own
    docstring ("write-only ... nothing reads these back yet")."""

    def setUp(self):
        self.staff = User.objects.create_user(username='staffer', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staffer', password='pw')
        self.seller = User.objects.create_user(username='seller1', password='pw', email='seller1@example.com')

    def test_empty_state_renders(self):
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(response, 'No staff actions recorded yet.')

    def test_action_appears_in_recent_activity(self):
        book = make_book(self.seller, title='Some Flagged Book', status='under_review')
        self.client.post(reverse('adminpanel:approve_book', args=[book.pk]))
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(response, 'Some Flagged Book')
        self.assertContains(response, 'Approved listing')
        activity = list(response.context['recent_activity'])
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0].actor, self.staff)

    def test_most_recent_action_appears_first(self):
        book1 = make_book(self.seller, title='First Book', status='under_review')
        book2 = make_book(self.seller, title='Second Book', status='under_review')
        self.client.post(reverse('adminpanel:approve_book', args=[book1.pk]))
        self.client.post(reverse('adminpanel:approve_book', args=[book2.pk]))
        response = self.client.get(reverse('adminpanel:dashboard'))
        activity = list(response.context['recent_activity'])
        self.assertEqual(activity[0].target_repr, 'Second Book')
        self.assertEqual(activity[1].target_repr, 'First Book')

    def test_capped_at_recent_activity_limit(self):
        from .views import RECENT_ACTIVITY_LIMIT
        for i in range(RECENT_ACTIVITY_LIMIT + 5):
            AuditLog.record(actor=self.staff, action='approve_book', target_repr=f'Book {i}', target_id=i)
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertEqual(len(response.context['recent_activity']), RECENT_ACTIVITY_LIMIT)

    def test_deleted_actor_shows_fallback(self):
        AuditLog.record(actor=None, action='approve_book', target_repr='Orphaned Action')
        response = self.client.get(reverse('adminpanel:dashboard'))
        self.assertContains(response, 'deleted user')
