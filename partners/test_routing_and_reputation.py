import io
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import User
from notifications.models import Notification

from . import assignment, badges
from .models import BuybackRequest, PartnerReview, ShopkeeperPartner

_MEDIA = tempfile.mkdtemp(prefix='bb-routing-media-')


def tearDownModule():
    shutil.rmtree(_MEDIA, ignore_errors=True)


def _png(name):
    buf = io.BytesIO()
    Image.new('RGB', (20, 20), 'blue').save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


BASE_LAT, BASE_LON = Decimal('26.846700'), Decimal('80.946200')


def make_partner(username, lat_offset=Decimal('0'), status='approved', **extra):
    user = User.objects.create_user(username, f'{username}@x.com', 'pw12345!')
    p = ShopkeeperPartner(
        user=user, shop_name=f'{username} books', shop_address='1 Road', shop_city='Lucknow', shop_pincode='226001',
        contact_phone='9999900000', status=status,
        shop_latitude=BASE_LAT + lat_offset, shop_longitude=BASE_LON, **extra,
    )
    p.owner_id_proof.save('id.png', _png('id.png'), save=False)
    p.shop_proof.save('shop.png', _png('shop.png'), save=False)
    p.save()
    return p


def make_lead(seller, **extra):
    data = dict(
        seller=seller, title='Physics Vol 1', pickup_address='5 Lane', pickup_city='Lucknow', pickup_pincode='226001',
        pickup_latitude=BASE_LAT, pickup_longitude=BASE_LON,
    )
    data.update(extra)
    return BuybackRequest.objects.create(**data)


@override_settings(MEDIA_ROOT=_MEDIA, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', LEAD_RESPONSE_MINUTES=120)
class LeadRoutingTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('seller', 'seller@x.com', 'pw12345!')
        self.near = make_partner('near', Decimal('0.00'))
        self.mid = make_partner('mid', Decimal('0.05'))
        self.far = make_partner('far', Decimal('0.09'))
        self.lead = make_lead(self.seller)

    def route(self):
        with self.captureOnCommitCallbacks(execute=True):
            assignment.route_new_lead(self.lead)
        self.lead.refresh_from_db()

    def login(self, partner):
        self.client.logout()
        self.client.login(username=partner.user.username, password='pw12345!')

    def test_new_lead_is_reserved_for_nearest_partner_and_they_are_notified(self):
        self.route()
        self.assertEqual(self.lead.assigned_partner, self.near)
        self.assertEqual(self.lead.assignment_round, 1)
        self.assertAlmostEqual((self.lead.assignment_expires_at - timezone.now()).total_seconds(), 7200, delta=30)
        self.assertTrue(Notification.objects.filter(user=self.near.user, title__startswith='Lead reserved').exists())
        self.assertFalse(Notification.objects.filter(user=self.mid.user).exists())

    def test_reserved_lead_hidden_from_other_partners_but_visible_to_assignee(self):
        self.route()
        self.login(self.mid)
        self.assertNotContains(self.client.get(reverse('partners:dashboard')), 'Physics Vol 1')
        self.assertEqual(self.client.get(reverse('partners:lead_detail', args=[self.lead.pk])).status_code, 404)
        self.login(self.near)
        r = self.client.get(reverse('partners:dashboard'))
        self.assertContains(r, 'Physics Vol 1')
        self.assertContains(r, 'Reserved for you')
        self.assertEqual(self.client.get(reverse('partners:lead_detail', args=[self.lead.pk])).status_code, 200)

    def test_other_partner_cannot_offer_on_a_reserved_lead(self):
        self.route()
        self.login(self.mid)
        self.client.post(reverse('partners:make_offer', args=[self.lead.pk]), {'offered_price': '50'})
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.status, self.lead.partner), ('pending', None))

    def test_assignee_offer_is_recorded_and_stops_the_clock(self):
        self.route()
        self.login(self.near)
        self.client.post(reverse('partners:make_offer', args=[self.lead.pk]), {'offered_price': '50'})
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.status, self.lead.partner), ('offer_made', self.near))
        self.assertIsNotNone(self.lead.offered_at)
        self.assertIsNone(self.lead.assignment_expires_at)
        self.assertEqual(assignment.sweep_expired(now=timezone.now() + timedelta(days=2)), 0)

    def test_nothing_moves_before_the_window_ends(self):
        self.route()
        self.assertEqual(assignment.sweep_expired(now=timezone.now() + timedelta(minutes=119)), 0)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_partner, self.near)

    def test_lapse_moves_to_next_nearest_and_notifies_both(self):
        self.route()
        with self.captureOnCommitCallbacks(execute=True):
            moved = assignment.sweep_expired(now=timezone.now() + timedelta(minutes=121))
        self.assertEqual(moved, 1)
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.assigned_partner, self.lead.assignment_round), (self.mid, 2))
        self.assertEqual(list(self.lead.skipped_partners.all()), [self.near])
        self.assertTrue(Notification.objects.filter(user=self.near.user, title__startswith='Lead passed on').exists())
        self.assertTrue(Notification.objects.filter(user=self.mid.user, title__startswith='Lead reserved').exists())

    def test_sweep_twice_does_not_double_advance(self):
        self.route()
        later = timezone.now() + timedelta(minutes=121)
        assignment.sweep_expired(now=later)
        assignment.sweep_expired(now=later)  # the new window runs from `later`, so a repeat sweep is a no-op
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assignment_round, 2)

    @override_settings(LEAD_MAX_ASSIGNMENT_ROUNDS=2)
    def test_after_max_rounds_lead_opens_to_everyone_in_range(self):
        self.route()
        t = timezone.now()
        assignment.sweep_expired(now=t + timedelta(minutes=121))      # near -> mid
        with self.captureOnCommitCallbacks(execute=True):
            assignment.sweep_expired(now=t + timedelta(minutes=300))  # mid -> open pool (round limit)
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.assigned_partner)
        # `far` never had an exclusive turn, so it gets the normal new-lead alert...
        self.assertTrue(Notification.objects.filter(user=self.far.user, title__startswith='New buyback lead').exists())
        # ...and a partner who lapsed can still bid on the open lead.
        self.login(self.near)
        self.client.post(reverse('partners:make_offer', args=[self.lead.pk]), {'offered_price': '40'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.partner, self.near)

    def test_single_partner_lapses_straight_into_open_pool(self):
        BuybackRequest.objects.all().delete()
        for p in (self.mid, self.far):
            p.delete()
        lead = make_lead(self.seller)
        assignment.route_new_lead(lead)
        lead.refresh_from_db()
        self.assertEqual(lead.assigned_partner, self.near)
        assignment.sweep_expired(now=timezone.now() + timedelta(minutes=121))
        lead.refresh_from_db()
        self.assertIsNone(lead.assigned_partner)

    def test_no_partners_in_range_leaves_lead_open(self):
        ShopkeeperPartner.objects.all().delete()
        assignment.route_new_lead(self.lead)
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.assigned_partner)
        self.assertEqual(self.lead.assignment_round, 0)

    def test_pass_hands_on_immediately(self):
        self.route()
        self.login(self.near)
        self.client.post(reverse('partners:pass_lead', args=[self.lead.pk]))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_partner, self.mid)
        self.assertIn(self.near, self.lead.skipped_partners.all())

    def test_cannot_pass_someone_elses_lead(self):
        self.route()
        self.login(self.mid)
        self.client.post(reverse('partners:pass_lead', args=[self.lead.pk]))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_partner, self.near)

    def test_pausing_or_suspension_releases_a_reserved_lead_immediately(self):
        self.route()
        ShopkeeperPartner.objects.filter(pk=self.near.pk).update(is_accepting_requests=False)
        self.assertEqual(assignment.sweep_expired(), 1)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_partner, self.mid)

    def test_paused_and_unapproved_partners_are_never_candidates(self):
        ShopkeeperPartner.objects.filter(pk=self.near.pk).update(is_accepting_requests=False)
        ShopkeeperPartner.objects.filter(pk=self.mid.pk).update(status='pending')
        self.assertEqual(assignment.candidate_partners(self.lead), [self.far])

    def test_sellers_own_partner_shop_is_excluded(self):
        own = ShopkeeperPartner.objects.get(pk=self.near.pk)
        lead = make_lead(own.user, title='Mine')
        self.assertNotIn(self.near, assignment.candidate_partners(lead))

    def test_rejected_offer_restarts_routing_without_that_partner(self):
        self.route()
        self.login(self.near)
        self.client.post(reverse('partners:make_offer', args=[self.lead.pk]), {'offered_price': '50'})
        self.login_as_seller = self.client.login(username='seller', password='pw12345!')
        self.client.post(reverse('partners:reject_offer', args=[self.lead.pk]))
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.status, self.lead.partner, self.lead.offered_at), ('pending', None, None))
        self.assertEqual(self.lead.assigned_partner, self.mid)
        self.assertEqual(self.lead.assignment_round, 1)   # counter restarted
        self.assertIn(self.near, self.lead.skipped_partners.all())

    def test_management_command(self):
        self.route()
        BuybackRequest.objects.filter(pk=self.lead.pk).update(assignment_expires_at=timezone.now() - timedelta(minutes=1))
        out = io.StringIO()
        call_command('reassign_stale_leads', stdout=out)
        self.assertIn('Reassigned 1 lead', out.getvalue())

    def test_dashboard_visit_sweeps_expired_leads(self):
        self.route()
        BuybackRequest.objects.filter(pk=self.lead.pk).update(assignment_expires_at=timezone.now() - timedelta(minutes=1))
        self.login(self.mid)
        self.assertContains(self.client.get(reverse('partners:dashboard')), 'Physics Vol 1')

    def test_seller_sees_routing_status(self):
        self.route()
        self.client.login(username='seller', password='pw12345!')
        self.assertContains(self.client.get(reverse('partners:buyback_detail', args=[self.lead.pk])), 'Sent to a nearby partner first')

    def test_time_left_label(self):
        self.route()
        self.assertEqual(assignment.time_left_label(self.lead), '2h 00m')
        self.assertEqual(assignment.time_left_label(self.lead, now=self.lead.assignment_expires_at - timedelta(minutes=5)), '5m')
        self.assertEqual(assignment.time_left_label(self.lead, now=self.lead.assignment_expires_at + timedelta(seconds=1)), 'expiring now')


def paid_buyback(seller, partner, **extra):
    data = dict(
        seller=seller, partner=partner, title='Chem', pickup_address='x', pickup_city='Lucknow',
        status='paid', final_price=Decimal('120'), completed_at=timezone.now(),
    )
    data.update(extra)
    return BuybackRequest.objects.create(**data)


@override_settings(MEDIA_ROOT=_MEDIA, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PartnerReviewTests(TestCase):
    def setUp(self):
        self.partner = make_partner('shopA')
        self.seller = User.objects.create_user('sellerx', 'sx@x.com', 'pw12345!')
        self.bb = paid_buyback(self.seller, self.partner)
        self.url = reverse('partners:review_partner', args=[self.bb.pk])
        self.client.login(username='sellerx', password='pw12345!')

    def test_seller_can_review_completed_buyback_once(self):
        self.client.post(self.url, {'rating': '5', 'comment': ' Prompt and fair '})
        r = PartnerReview.objects.get()
        self.assertEqual((r.rating, r.comment, r.reviewer, r.partner), (5, 'Prompt and fair', self.seller, self.partner))
        self.assertTrue(Notification.objects.filter(user=self.partner.user, title__contains='5-star').exists())
        self.client.post(self.url, {'rating': '1', 'comment': 'changed my mind'})
        self.assertEqual(PartnerReview.objects.count(), 1)
        self.assertEqual(PartnerReview.objects.get().rating, 5)

    def test_invalid_ratings_rejected(self):
        for bad in ['0', '6', 'abc', '']:
            self.client.post(self.url, {'rating': bad})
        self.assertEqual(PartnerReview.objects.count(), 0)

    def test_only_the_seller_of_a_paid_buyback_can_review(self):
        other = User.objects.create_user('stranger', 'st@x.com', 'pw12345!')
        self.client.login(username='stranger', password='pw12345!')
        self.assertEqual(self.client.post(self.url, {'rating': '1'}).status_code, 404)
        self.client.login(username=self.partner.user.username, password='pw12345!')   # the partner can't rate themselves
        self.assertEqual(self.client.post(self.url, {'rating': '5'}).status_code, 404)
        BuybackRequest.objects.filter(pk=self.bb.pk).update(status='accepted')
        self.client.login(username='sellerx', password='pw12345!')
        self.assertEqual(self.client.post(self.url, {'rating': '5'}).status_code, 404)
        self.assertEqual(PartnerReview.objects.count(), 0)

    def test_review_form_then_review_shown_on_seller_page(self):
        detail = reverse('partners:buyback_detail', args=[self.bb.pk])
        self.assertContains(self.client.get(detail), 'Submit review')
        self.client.post(self.url, {'rating': '4', 'comment': 'ok'})
        r = self.client.get(detail)
        self.assertContains(r, 'Your review of')
        self.assertNotContains(r, 'Submit review')

    def test_hidden_reviews_are_excluded_from_stats(self):
        for i in range(3):
            s = User.objects.create_user(f'rv{i}', f'rv{i}@x.com', 'pw12345!')
            PartnerReview.objects.create(partner=self.partner, buyback_request=paid_buyback(s, self.partner), reviewer=s, rating=5)
        s = User.objects.create_user('troll', 'troll@x.com', 'pw12345!')
        bad = PartnerReview.objects.create(partner=self.partner, buyback_request=paid_buyback(s, self.partner), reviewer=s, rating=1)
        self.assertEqual(self.partner.review_stats(), {'average': 4.0, 'count': 4})
        PartnerReview.objects.filter(pk=bad.pk).update(is_hidden=True)
        self.assertEqual(self.partner.review_stats(), {'average': 5.0, 'count': 3})


class BadgeRuleTests(TestCase):
    def stats(self, completed=0, rating=None, n=0, median=None):
        return {'completed': completed, 'rating': rating, 'review_count': n, 'median_response_minutes': median}

    def codes(self, **kw):
        return [b['code'] for b in badges.badges_for_stats(self.stats(**kw))]

    def test_no_activity_no_badges(self):
        self.assertEqual(self.codes(), [])

    def test_volume_badges(self):
        self.assertEqual(self.codes(completed=1), ['first_pickup'])
        self.assertEqual(self.codes(completed=10), ['first_pickup', 'active_buyer'])
        self.assertEqual(self.codes(completed=25), ['first_pickup', 'active_buyer', 'pro_partner'])

    def test_rating_badges_need_enough_reviews(self):
        self.assertEqual(self.codes(rating=5.0, n=4), [])
        self.assertEqual(self.codes(rating=4.5, n=5), ['trusted'])
        self.assertEqual(self.codes(rating=4.4, n=50), [])
        self.assertEqual(self.codes(rating=4.8, n=15), ['trusted', 'top_rated'])
        self.assertEqual(self.codes(rating=4.7, n=40), ['trusted'])

    def test_quick_responder(self):
        self.assertEqual(self.codes(median=45), ['quick_responder'])
        self.assertEqual(self.codes(median=60), ['quick_responder'])
        self.assertEqual(self.codes(median=61), [])
        self.assertEqual(self.codes(median=None), [])

    def test_hints(self):
        self.assertTrue(any('First Pickup' in h for h in badges.next_milestones(self.stats())))
        self.assertTrue(any('Trusted Partner' in h for h in badges.next_milestones(self.stats(completed=30, rating=4.0, n=9))))


@override_settings(MEDIA_ROOT=_MEDIA, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ReputationIntegrationTests(TestCase):
    def setUp(self):
        self.partner = make_partner('shopB')
        self.seller = User.objects.create_user('sellery', 'sy@x.com', 'pw12345!')

    def test_median_response_time_needs_five_offers_and_drives_badge(self):
        now = timezone.now()
        for minutes in (10, 20, 30, 40):
            bb = paid_buyback(self.seller, self.partner)
            BuybackRequest.objects.filter(pk=bb.pk).update(created_at=now - timedelta(minutes=minutes), offered_at=now)
        self.assertIsNone(badges.compute_stats(self.partner)['median_response_minutes'])
        bb = paid_buyback(self.seller, self.partner)
        BuybackRequest.objects.filter(pk=bb.pk).update(created_at=now - timedelta(minutes=50), offered_at=now)
        self.assertEqual(badges.compute_stats(self.partner)['median_response_minutes'], 30)
        self.assertIn('quick_responder', [b['code'] for b in badges.reputation(self.partner)['badges']])

    def test_profile_page_shows_public_info_only(self):
        s = User.objects.create_user('alicebob', 'ab@x.com', 'pw12345!')
        PartnerReview.objects.create(partner=self.partner, buyback_request=paid_buyback(s, self.partner), reviewer=s, rating=5, comment='Great shop')
        h = User.objects.create_user('hideme', 'h@x.com', 'pw12345!')
        PartnerReview.objects.create(partner=self.partner, buyback_request=paid_buyback(h, self.partner), reviewer=h, rating=1, comment='SECRET-HIDDEN', is_hidden=True)
        self.client.login(username='sellery', password='pw12345!')
        r = self.client.get(reverse('partners:profile', args=[self.partner.pk]))
        self.assertContains(r, 'shopB books')
        self.assertContains(r, 'Great shop')
        self.assertNotContains(r, 'SECRET-HIDDEN')
        self.assertNotContains(r, 'alicebob')          # reviewer name is masked
        self.assertContains(r, 'a****')
        self.assertNotContains(r, '9999900000')        # no phone
        self.assertNotContains(r, '1 Road')            # no address

    def test_profile_only_for_approved_partners_and_logged_in_users(self):
        pending = make_partner('pendshop', status='pending')
        self.assertEqual(self.client.get(reverse('partners:profile', args=[self.partner.pk])).status_code, 302)  # login required
        self.client.login(username='sellery', password='pw12345!')
        self.assertEqual(self.client.get(reverse('partners:profile', args=[pending.pk])).status_code, 404)

    def test_dashboard_and_offer_card_show_reputation(self):
        s = User.objects.create_user('rater', 'r@x.com', 'pw12345!')
        PartnerReview.objects.create(partner=self.partner, buyback_request=paid_buyback(s, self.partner), reviewer=s, rating=4)
        self.client.login(username='shopB', password='pw12345!')
        r = self.client.get(reverse('partners:dashboard'))
        self.assertContains(r, 'First Pickup')
        self.assertContains(r, 'How sellers see your shop')
        lead = make_lead(self.seller, status='offer_made', partner=self.partner, offered_price=Decimal('80'))
        self.client.login(username='sellery', password='pw12345!')
        r = self.client.get(reverse('partners:buyback_detail', args=[lead.pk]))
        self.assertContains(r, 'First Pickup')
        self.assertContains(r, reverse('partners:profile', args=[self.partner.pk]))
