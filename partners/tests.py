import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User
from notifications.models import Notification
from partners.models import ShopkeeperPartner


def _img(name='p.png'):
    buf = io.BytesIO()
    Image.new('RGB', (40, 40), 'blue').save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def _make_partner(user, status='pending', **extra):
    p = ShopkeeperPartner(
        user=user, shop_name='Old Books Hub', shop_address='1 Road', shop_city='Lucknow',
        shop_pincode='226001', contact_phone='9999999999', status=status, **extra,
    )
    p.owner_id_proof.save('id.png', _img(), save=False)
    p.shop_proof.save('shop.png', _img(), save=False)
    p.save()
    return p


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PartnerDecisionNotificationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@x.com', 'pw12345!', is_staff=True, is_superuser=True)
        self.applicant = User.objects.create_user('shop1', 'shop1@x.com', 'pw12345!')
        self.partner = _make_partner(self.applicant)
        self.client.login(username='boss', password='pw12345!')

    def test_approval_notifies_applicant(self):
        self.client.post(reverse('adminpanel:approve_partner', args=[self.partner.pk]))
        n = Notification.objects.get(user=self.applicant)
        self.assertIn('approved', n.title.lower())
        self.assertEqual(n.url, reverse('partners:dashboard'))

    def test_rejection_notifies_applicant_with_reason(self):
        self.client.post(reverse('adminpanel:reject_partner', args=[self.partner.pk]), {'reason': 'ID photo unreadable'})
        n = Notification.objects.get(user=self.applicant)
        self.assertIn('ID photo unreadable', n.body)
        self.assertIn('reapply=1', n.url)


class PartnerReapplyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('shop2', 's2@x.com', 'pw12345!')
        self.client.login(username='shop2', password='pw12345!')

    def _payload(self, **over):
        data = dict(
            shop_name='Fixed Shop', shop_address='2 Lane', shop_city='Lucknow', shop_pincode='226002',
            contact_phone='9888888888', gstin='', owner_aadhaar_number='234567890123',
            owner_id_proof=_img('id2.png'), shop_proof=_img('shop2.png'),
        )
        data.update(over)
        return data

    def test_rejected_applicant_sees_form_prefilled_with_reason(self):
        _make_partner(self.user, status='rejected', rejection_reason='Blurry proof')
        r = self.client.get(reverse('partners:apply') + '?reapply=1')
        self.assertTemplateUsed(r, 'partners/apply.html')
        self.assertContains(r, 'Blurry proof')
        self.assertContains(r, 'Old Books Hub')

    def test_reapply_resets_to_pending_and_clears_reason(self):
        _make_partner(self.user, status='rejected', rejection_reason='Blurry proof')
        r = self.client.post(reverse('partners:apply'), self._payload())
        self.assertRedirects(r, reverse('partners:apply'))
        p = ShopkeeperPartner.objects.get(user=self.user)
        self.assertEqual(ShopkeeperPartner.objects.filter(user=self.user).count(), 1)
        self.assertEqual((p.status, p.rejection_reason, p.shop_name), ('pending', '', 'Fixed Shop'))

    def test_pending_applicant_does_not_get_the_reapply_flow(self):
        # ?reapply=1 is only for rejected applications; a pending one sees its status page
        # (which links to the separate ?edit=1 flow - see PartnerPendingEditTests).
        _make_partner(self.user, status='pending')
        r = self.client.get(reverse('partners:apply') + '?reapply=1')
        self.assertTemplateUsed(r, 'partners/application_status.html')
        self.assertContains(r, '?edit=1')

    def test_approved_applicant_cannot_reapply(self):
        _make_partner(self.user, status='approved')
        self.client.post(reverse('partners:apply'), self._payload())
        p = ShopkeeperPartner.objects.get(user=self.user)
        self.assertEqual((p.status, p.shop_name), ('approved', 'Old Books Hub'))

    def test_rejected_status_page_offers_reapply_button(self):
        _make_partner(self.user, status='rejected')
        r = self.client.get(reverse('partners:apply'))
        self.assertContains(r, 'reapply=1')


from decimal import Decimal

from partners.utils import estimate_buyback_quote


class BuybackQuoteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('seller1', 's@x.com', 'pw12345!')
        self.url = reverse('partners:buyback_quote')

    def _quote(self, **over):
        args = dict(title='Concepts of Physics', condition='good', original_price=Decimal('500'))
        args.update(over)
        return estimate_buyback_quote(**args)

    def _live_partner(self, name, city='Lucknow', margin='25.0', **kw):
        u = User.objects.create_user(name, f'{name}@x.com', 'pw12345!')
        p = _make_partner(u, status='approved', **kw)
        ShopkeeperPartner.objects.filter(pk=p.pk).update(shop_city=city, target_margin_percent=Decimal(margin))
        return p

    def test_offer_is_below_resale_and_range_is_ordered(self):
        q = self._quote()
        self.assertLess(q['offer_mid'], q['market_price'])
        self.assertLessEqual(q['offer_low'], q['offer_mid'])
        self.assertLessEqual(q['offer_mid'], q['offer_high'])
        self.assertGreaterEqual(q['offer_low'], 1)

    def test_offers_rounded_to_five_rupees(self):
        q = self._quote()
        for key in ('offer_low', 'offer_mid', 'offer_high'):
            self.assertEqual(q[key] % 5, 0)

    def test_higher_partner_margins_lower_the_quote(self):
        before = self._quote()['offer_mid']
        self._live_partner('greedy', margin='60.0')
        after = self._quote()['offer_mid']
        self.assertLess(after, before)

    def test_quantity_multiplies_totals(self):
        q = self._quote(quantity=3)
        self.assertEqual(q['total_mid'], q['offer_mid'] * 3)
        self.assertEqual(q['quantity'], 3)

    def test_counts_only_live_partners_in_city_case_insensitive(self):
        self._live_partner('p1', city='Lucknow')
        pending_user = User.objects.create_user('pend', 'pend@x.com', 'pw12345!')
        _make_partner(pending_user, status='pending')
        closed = self._live_partner('p3', city='Lucknow')
        ShopkeeperPartner.objects.filter(pk=closed.pk).update(is_accepting_requests=False)
        self._live_partner('p4', city='Delhi')
        q = self._quote(city='lucknow')
        self.assertEqual(q['partners_in_city'], 1)
        self.assertEqual(q['partners_total'], 2)

    def test_ask_note_above_within_below(self):
        q = self._quote()
        above = self._quote(expected_price=q['offer_high'] + 100)['note']
        within = self._quote(expected_price=q['offer_mid'])['note']
        self.assertIn('above', above)
        self.assertIn('within', within)
        if q['offer_low'] > 1:
            self.assertIn('below', self._quote(expected_price=q['offer_low'] - 1)['note'])
        self.assertEqual(self._quote()['note'], '')

    def test_endpoint_requires_login_and_post(self):
        self.assertEqual(self.client.post(self.url, {'title': 'x'}).status_code, 302)
        self.client.login(username='seller1', password='pw12345!')
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_endpoint_requires_title(self):
        self.client.login(username='seller1', password='pw12345!')
        r = self.client.post(self.url, {'title': '  '})
        self.assertEqual(r.status_code, 400)
        self.assertIn('error', r.json())

    def test_endpoint_returns_quote_and_ignores_bad_numbers(self):
        self.client.login(username='seller1', password='pw12345!')
        r = self.client.post(self.url, {
            'title': 'Concepts of Physics', 'condition': 'good', 'original_price': 'abc',
            'quantity': '999', 'publication_year': 'x', 'expected_price': '150', 'pickup_city': 'Lucknow',
        })
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['quantity'], 50)  # capped
        self.assertTrue(d['note'])
        self.assertGreater(Decimal(d['offer_high']), 0)

    def test_sell_page_renders_quote_card(self):
        User.objects.filter(pk=self.user.pk).update(email_verified=True)
        self.client.login(username='seller1', password='pw12345!')
        r = self.client.get(reverse('partners:sell_to_bookbridge'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Instant Buyback Quote')
        self.assertContains(r, reverse('partners:buyback_quote'))
        self.assertNotContains(r, 'AI Price Recommendation')


# --- Pickup scheduling + handover OTP -------------------------------------------

from datetime import timedelta  # noqa: E402

from django.core import mail  # noqa: E402
from django.utils import timezone  # noqa: E402

from partners.models import MAX_PICKUP_OTP_ATTEMPTS, BuybackRequest  # noqa: E402


def _accepted_buyback(seller, partner, **extra):
    b = BuybackRequest.objects.create(
        seller=seller, partner=partner, title='Physics Vol 1', pickup_address='5 Street',
        pickup_city='Lucknow', status='accepted', offered_price=150, **extra,
    )
    b.issue_pickup_otp()
    return b


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PickupSchedulingTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw12345!')
        self.shop_user = User.objects.create_user('shop', 'shop@x.com', 'pw12345!')
        self.other = User.objects.create_user('rando', 'r@x.com', 'pw12345!')
        self.partner = _make_partner(self.shop_user, status='approved')
        self.b = _accepted_buyback(self.seller, self.partner)
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def _propose(self, user, **over):
        self.client.login(username=user, password='pw12345!')
        data = {'pickup_date': self.tomorrow.isoformat(), 'pickup_slot': 'morning'}
        data.update(over)
        return self.client.post(reverse('partners:propose_pickup', args=[self.b.pk]), data)

    def test_accept_offer_mints_otp(self):
        b = BuybackRequest.objects.create(
            seller=self.seller, partner=self.partner, title='X', pickup_address='a', pickup_city='c',
            status='offer_made', offered_price=100,
        )
        self.client.login(username='sel', password='pw12345!')
        self.client.post(reverse('partners:accept_offer', args=[b.pk]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'accepted')
        self.assertRegex(b.pickup_otp, r'^\d{6}$')

    def test_propose_then_other_side_confirms(self):
        self._propose('sel')
        self.b.refresh_from_db()
        self.assertEqual(self.b.pickup_date, self.tomorrow)
        self.assertFalse(self.b.pickup_confirmed)
        self.assertTrue(Notification.objects.filter(user=self.shop_user, title__startswith='Pickup proposed').exists())

        # proposer cannot confirm their own proposal
        self.client.post(reverse('partners:confirm_pickup', args=[self.b.pk]))
        self.b.refresh_from_db()
        self.assertFalse(self.b.pickup_confirmed)

        self.client.login(username='shop', password='pw12345!')
        self.client.post(reverse('partners:confirm_pickup', args=[self.b.pk]))
        self.b.refresh_from_db()
        self.assertTrue(self.b.pickup_confirmed)
        self.assertTrue(Notification.objects.filter(user=self.seller, title__startswith='Pickup confirmed').exists())

    def test_repropose_resets_confirmation(self):
        self._propose('sel')
        self.client.login(username='shop', password='pw12345!')
        self.client.post(reverse('partners:confirm_pickup', args=[self.b.pk]))
        self._propose('shop', pickup_slot='evening')
        self.b.refresh_from_db()
        self.assertFalse(self.b.pickup_confirmed)
        self.assertEqual(self.b.pickup_slot, 'evening')
        self.assertEqual(self.b.pickup_proposed_by, self.shop_user)

    def test_rejects_past_and_too_far_dates(self):
        self._propose('sel', pickup_date=(timezone.localdate() - timedelta(days=1)).isoformat())
        self._propose('sel', pickup_date=(timezone.localdate() + timedelta(days=60)).isoformat())
        self.b.refresh_from_db()
        self.assertIsNone(self.b.pickup_date)

    def test_outsider_cannot_schedule(self):
        resp = self._propose('rando')
        self.assertEqual(resp.status_code, 404)

    def test_seller_sees_otp_partner_does_not(self):
        self.client.login(username='sel', password='pw12345!')
        self.assertContains(self.client.get(reverse('partners:buyback_detail', args=[self.b.pk])), self.b.pickup_otp)
        self.client.login(username='shop', password='pw12345!')
        resp = self.client.get(reverse('partners:lead_detail', args=[self.b.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.b.pickup_otp)

    def _complete(self, otp):
        self.client.login(username='shop', password='pw12345!')
        return self.client.post(
            reverse('partners:mark_paid', args=[self.b.pk]),
            {'final_price': '140', 'pickup_otp': otp},
        )

    def test_mark_paid_requires_correct_otp(self):
        self._complete('000000' if self.b.pickup_otp != '000000' else '111111')
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, 'accepted')
        self.assertEqual(self.b.pickup_otp_attempts, 1)

        self._complete('')
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, 'accepted')
        self.assertEqual(self.b.pickup_otp_attempts, 1)  # blank doesn't burn an attempt

        self._complete(self.b.pickup_otp)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, 'paid')
        self.assertIsNotNone(self.b.pickup_otp_verified_at)
        self.assertEqual(self.b.commission.amount, 14)

    def test_otp_locks_out_then_seller_reissue_unlocks(self):
        wrong = '000000' if self.b.pickup_otp != '000000' else '111111'
        for _ in range(MAX_PICKUP_OTP_ATTEMPTS):
            self._complete(wrong)
        old = self.b.pickup_otp
        self._complete(old)  # correct code, but locked
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, 'accepted')
        self.assertTrue(self.b.pickup_otp_locked)

        self.client.login(username='sel', password='pw12345!')
        self.client.post(reverse('partners:reissue_pickup_otp', args=[self.b.pk]))
        self.b.refresh_from_db()
        self.assertFalse(self.b.pickup_otp_locked)
        self._complete(self.b.pickup_otp)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, 'paid')

    def test_legacy_request_without_otp_can_still_complete(self):
        self.b.pickup_otp = ''
        self.b.save()
        self._complete('')
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, 'paid')

    def test_reopen_clears_pickup_state(self):
        self._propose('sel')
        self.b.reopen()
        self.b.refresh_from_db()
        self.assertEqual((self.b.pickup_otp, self.b.pickup_slot, self.b.pickup_confirmed), ('', '', False))
        self.assertIsNone(self.b.pickup_date)


import shutil
import tempfile

_TMP_MEDIA = tempfile.mkdtemp(prefix='bb-test-media-')


def tearDownModule():
    shutil.rmtree(_TMP_MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=_TMP_MEDIA)
class PartnerApplicationValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('shop3', 's3@x.com', 'pw12345!')
        self.client.login(username='shop3', password='pw12345!')

    def payload(self, **over):
        data = dict(
            shop_name='Valid Shop', shop_address='3 Lane', shop_city='Lucknow', shop_pincode='226003',
            contact_phone='9777777777', gstin='', id_type='aadhaar', owner_aadhaar_number='234567890123',
            owner_pan_number='', owner_id_proof=_img('id3.png'), shop_proof=_img('shop3.png'),
        )
        data.update(over)
        return data

    def test_valid_gstin_saved_normalised(self):
        self.client.post(reverse('partners:apply'), self.payload(gstin=' 27aapfu0939f1zv '))
        self.assertEqual(ShopkeeperPartner.objects.get(user=self.user).gstin, '27AAPFU0939F1ZV')

    def test_bad_checksum_gstin_blocks_application(self):
        r = self.client.post(reverse('partners:apply'), self.payload(gstin='27AAPFU0939F1ZX'))
        self.assertContains(r, 'checksum')
        self.assertFalse(ShopkeeperPartner.objects.filter(user=self.user).exists())

    def test_gstin_already_used_by_another_application(self):
        other = User.objects.create_user('other', 'o@x.com', 'pw12345!')
        _make_partner(other, status='approved', gstin='27AAPFU0939F1ZV')
        r = self.client.post(reverse('partners:apply'), self.payload(gstin='27AAPFU0939F1ZV'))
        self.assertContains(r, 'already uses this GSTIN')

    def test_gstin_of_a_rejected_application_can_be_reused(self):
        other = User.objects.create_user('other', 'o@x.com', 'pw12345!')
        _make_partner(other, status='rejected', gstin='27AAPFU0939F1ZV')
        self.client.post(reverse('partners:apply'), self.payload(gstin='27AAPFU0939F1ZV'))
        self.assertTrue(ShopkeeperPartner.objects.filter(user=self.user, gstin='27AAPFU0939F1ZV').exists())

    def test_pan_application_stores_only_hash_and_last4(self):
        self.client.post(reverse('partners:apply'), self.payload(id_type='pan', owner_aadhaar_number='', owner_pan_number='abcpe1234f'))
        p = ShopkeeperPartner.objects.get(user=self.user)
        self.assertEqual((p.id_type, p.owner_pan_last4, p.owner_aadhaar_hash), ('pan', '234F', ''))
        self.assertNotIn('ABCPE1234F', p.owner_pan_hash)
        self.assertEqual(p.id_number_hint, 'PAN ending 234F')

    def test_invalid_pan_rejected(self):
        r = self.client.post(reverse('partners:apply'), self.payload(id_type='pan', owner_aadhaar_number='', owner_pan_number='ABCDE1234F'))
        self.assertContains(r, 'valid 10-character PAN')
        self.assertFalse(ShopkeeperPartner.objects.filter(user=self.user).exists())

    def test_missing_number_for_chosen_type(self):
        r = self.client.post(reverse('partners:apply'), self.payload(id_type='pan', owner_aadhaar_number='234567890123', owner_pan_number=''))
        self.assertContains(r, 'Enter the owner')
        r = self.client.post(reverse('partners:apply'), self.payload(owner_aadhaar_number=''))
        self.assertContains(r, 'Enter the owner')

    def test_id_type_defaults_to_aadhaar_for_old_clients(self):
        data = self.payload(); data.pop('id_type'); data.pop('owner_pan_number')
        self.client.post(reverse('partners:apply'), data)
        self.assertEqual(ShopkeeperPartner.objects.get(user=self.user).id_type, 'aadhaar')

    def test_non_image_proof_rejected(self):
        bad = SimpleUploadedFile('id.png', b'definitely not a png', content_type='image/png')
        r = self.client.post(reverse('partners:apply'), self.payload(owner_id_proof=bad))
        self.assertEqual(r.status_code, 200)   # re-rendered with an error, nothing saved
        self.assertTrue(r.context['form'].errors.get('owner_id_proof'))
        self.assertFalse(ShopkeeperPartner.objects.filter(user=self.user).exists())

    def test_oversized_proof_rejected_with_clear_message(self):
        from accounts.validators import MAX_PROOF_BYTES
        big = SimpleUploadedFile('id.jpg', b'0' * (MAX_PROOF_BYTES + 1), content_type='image/jpeg')
        r = self.client.post(reverse('partners:apply'), self.payload(owner_id_proof=big))
        self.assertTrue(r.context['form'].errors.get('owner_id_proof'))

    def test_heic_photo_is_accepted_through_the_real_form_and_stored_as_jpeg(self):
        from accounts.validators import HEIC_SUPPORTED
        if not HEIC_SUPPORTED:
            self.skipTest('pillow-heif not installed')
        buf = io.BytesIO()
        Image.new('RGB', (64, 64), 'red').save(buf, 'HEIF')
        heic = SimpleUploadedFile('IMG_0042.HEIC', buf.getvalue(), content_type='image/heic')
        r = self.client.post(reverse('partners:apply'), self.payload(owner_id_proof=heic))
        self.assertRedirects(r, reverse('partners:apply'))
        p = ShopkeeperPartner.objects.get(user=self.user)
        self.assertTrue(p.owner_id_proof.name.endswith('.jpg'))
        self.assertEqual(Image.open(p.owner_id_proof.path).format, 'JPEG')

    def test_apply_page_shows_guidance_and_preview_hooks(self):
        r = self.client.get(reverse('partners:apply'))
        for needle in ['HEIC', 'previewIdProof', 'previewShopProof', 'upload-preview.js', 'accept="image/jpeg']:
            self.assertContains(r, needle)


@override_settings(MEDIA_ROOT=_TMP_MEDIA)
class PartnerPendingEditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('shop4', 's4@x.com', 'pw12345!')
        self.client.login(username='shop4', password='pw12345!')
        self.partner = _make_partner(self.user, status='pending', owner_aadhaar_last4='9012', owner_aadhaar_hash='hashed')
        self.url = reverse('partners:apply')

    def edit_payload(self, **over):
        data = dict(
            shop_name='Old Books Hub', shop_address='1 Road', shop_city='Lucknow', shop_pincode='226001',
            contact_phone='9999999999', gstin='', id_type='aadhaar', owner_aadhaar_number='', owner_pan_number='',
        )
        data.update(over)
        return data

    def test_edit_form_is_prefilled_and_shows_saved_documents(self):
        r = self.client.get(self.url + '?edit=1')
        self.assertTemplateUsed(r, 'partners/apply.html')
        self.assertContains(r, 'Editing your pending application')
        self.assertContains(r, 'data-saved-url')
        self.assertContains(r, 'Aadhaar ending 9012')

    def test_details_can_change_without_resubmitting_id_or_photos(self):
        r = self.client.post(self.url, self.edit_payload(shop_name='Better Name'))
        self.assertRedirects(r, self.url)
        p = ShopkeeperPartner.objects.get(pk=self.partner.pk)
        self.assertEqual((p.status, p.shop_name, p.owner_aadhaar_hash, p.owner_aadhaar_last4), ('pending', 'Better Name', 'hashed', '9012'))
        self.assertEqual(p.owner_id_proof.name, self.partner.owner_id_proof.name)
        self.assertIsNotNone(p.last_edited_at)

    def test_replacing_a_document_swaps_the_file_and_deletes_the_old_one(self):
        old_name = self.partner.owner_id_proof.name
        storage = self.partner.owner_id_proof.storage
        self.assertTrue(storage.exists(old_name))
        self.client.post(self.url, self.edit_payload(owner_id_proof=_img('newid.png')))
        p = ShopkeeperPartner.objects.get(pk=self.partner.pk)
        self.assertNotEqual(p.owner_id_proof.name, old_name)
        self.assertTrue(storage.exists(p.owner_id_proof.name))
        self.assertFalse(storage.exists(old_name))
        self.assertEqual(p.shop_proof.name, self.partner.shop_proof.name)   # untouched one stays

    def test_switching_id_type_requires_the_new_number(self):
        r = self.client.post(self.url, self.edit_payload(id_type='pan'))
        self.assertContains(r, 'Enter the owner')
        r = self.client.post(self.url, self.edit_payload(id_type='pan', owner_pan_number='ABCPE1234F'))
        self.assertRedirects(r, self.url)
        p = ShopkeeperPartner.objects.get(pk=self.partner.pk)
        self.assertEqual((p.id_type, p.owner_pan_last4, p.owner_aadhaar_hash), ('pan', '234F', ''))

    def test_bad_values_are_still_validated_while_editing(self):
        r = self.client.post(self.url, self.edit_payload(gstin='27AAPFU0939F1ZX'))
        self.assertContains(r, 'checksum')
        self.assertEqual(ShopkeeperPartner.objects.get(pk=self.partner.pk).gstin, '')

    def test_edit_is_refused_once_the_application_was_reviewed(self):
        ShopkeeperPartner.objects.filter(pk=self.partner.pk).update(status='approved')
        self.client.post(self.url, self.edit_payload(shop_name='Sneaky'))
        self.assertEqual(ShopkeeperPartner.objects.get(pk=self.partner.pk).shop_name, 'Old Books Hub')
        for status in ('rejected', 'suspended'):
            ShopkeeperPartner.objects.filter(pk=self.partner.pk).update(status=status)
        # approved + suspended never see an edit form
        ShopkeeperPartner.objects.filter(pk=self.partner.pk).update(status='suspended')
        r = self.client.get(self.url + '?edit=1')
        self.assertTemplateUsed(r, 'partners/application_status.html')

    def test_status_page_offers_edit_while_pending(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'Edit details or replace documents')
        ShopkeeperPartner.objects.filter(pk=self.partner.pk).update(status='approved')
        self.assertNotContains(self.client.get(self.url), 'Edit details or replace documents')
