import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import User
from adminpanel.models import AuditLog
from books.models import Book, Category
from notifications.models import Notification
from orders.models import Order, Payment, ReturnRequest
from payouts import services as payout_services
from payouts.models import Payout
from wallet.models import WalletTransaction
from wallet.services import get_wallet

from . import services
from .models import Dispute, DisputeEvidence, DisputeMessage

LOCMEM = override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', RATE_LIMITING_ENABLED=False)

LONG_TEXT = 'The pages are missing from the middle of the book and the cover is torn.'


def png_upload(name='proof.png'):
    buf = io.BytesIO()
    Image.new('RGB', (40, 30), (200, 30, 30)).save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def make_order(seller, buyer, *, price=500, status='delivered', payment_status='released', method='upi',
               delivered_days_ago=1, placed_days_ago=3, digital=False, razorpay_id=''):
    category, _ = Category.objects.get_or_create(name='Test Category')
    book = Book.objects.create(seller=seller, category=category, title='Some Book', price=price,
                               listing_type='digital' if digital else 'physical')
    order = Order.objects.create(
        buyer=buyer, book=book, final_price=price, delivery_charge=40, platform_commission_percent=10,
        payment_method=method, status=status, delivery_address='1 MG Road', delivery_city='Kanpur', delivery_pincode='208001',
    )
    Payment.objects.create(
        order=order, amount=order.total_amount, method=method, status=payment_status, razorpay_payment_id=razorpay_id,
    )
    fields = {'created_at': timezone.now() - timedelta(days=placed_days_ago)}
    if status == 'delivered':
        fields['delivered_at'] = timezone.now() - timedelta(days=delivered_days_ago)
    Order.objects.filter(pk=order.pk).update(**fields)
    order.refresh_from_db()
    return order


@LOCMEM
class DisputeTestBase(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw')
        self.buyer = User.objects.create_user('buy', 'buy@x.com', 'pw')
        self.stranger = User.objects.create_user('nosy', 'nosy@x.com', 'pw')
        self.staff = User.objects.create_user('boss', 'boss@x.com', 'pw', is_staff=True, is_superuser=True)

    def open(self, order, **kw):
        defaults = dict(category='missing_pages', description=LONG_TEXT, desired_resolution='full_refund')
        defaults.update(kw)
        return services.open_dispute(order, order.buyer, **defaults)

    def paid_order(self, **kw):
        order = make_order(self.seller, self.buyer, **kw)
        payout_services.ensure_payout(order)
        return Order.objects.get(pk=order.pk)


class EligibilityTests(DisputeTestBase):
    def test_delivered_order_can_be_disputed(self):
        self.assertEqual(services.blocked_reason(self.paid_order()), '')

    def test_placed_and_cancelled_orders_cannot(self):
        for status in ('placed', 'accepted', 'cancelled', 'returned'):
            order = make_order(self.seller, self.buyer, status=status)
            self.assertIn('shipped', services.blocked_reason(order), status)

    def test_window_after_delivery(self):
        old = make_order(self.seller, self.buyer, delivered_days_ago=services.DISPUTE_WINDOW_DAYS + 1)
        self.assertIn('window', services.blocked_reason(old))
        fresh = make_order(self.seller, self.buyer, delivered_days_ago=services.DISPUTE_WINDOW_DAYS - 1)
        self.assertEqual(services.blocked_reason(fresh), '')

    def test_not_received_needs_the_waiting_period(self):
        early = make_order(self.seller, self.buyer, status='shipped', placed_days_ago=2)
        self.assertIn('days after they were placed', services.blocked_reason(early))
        late = make_order(self.seller, self.buyer, status='shipped', placed_days_ago=services.NOT_RECEIVED_AFTER_DAYS + 1)
        self.assertEqual(services.blocked_reason(late), '')
        self.assertEqual(services.allowed_categories(late), ['not_received'])

    def test_digital_orders_only_get_digital_categories(self):
        order = make_order(self.seller, self.buyer, digital=True)
        cats = services.allowed_categories(order)
        self.assertIn('digital_file', cats)
        self.assertNotIn('damaged', cats)
        self.assertNotIn('digital_file', services.allowed_categories(make_order(self.seller, self.buyer)))

    def test_one_dispute_per_order_but_withdrawn_can_be_redone(self):
        order = self.paid_order()
        d = self.open(order)
        with self.assertRaises(services.DisputeError):
            self.open(order)
        services.withdraw(d.pk, self.buyer)
        self.assertEqual(services.blocked_reason(order), '')
        self.open(order)  # allowed again

    def test_already_refunded_order_is_blocked(self):
        order = self.paid_order(payment_status='refunded')
        self.assertIn('already been refunded', services.blocked_reason(order))


class OpenDisputeTests(DisputeTestBase):
    def test_open_creates_thread_holds_payout_and_notifies_seller(self):
        order = self.paid_order()
        with self.captureOnCommitCallbacks(execute=True):
            d = self.open(order, files=[png_upload()])
        self.assertEqual(d.status, 'open')
        self.assertEqual(d.seller, self.seller)
        self.assertTrue(d.payout_held)
        self.assertEqual(Payout.objects.get(order=order).status, 'on_hold')
        self.assertEqual(d.messages.filter(role='buyer').count(), 1)
        self.assertEqual(d.evidence.count(), 1)
        self.assertTrue(Notification.objects.filter(user=self.seller, title__icontains='Dispute opened').exists())
        self.assertTrue(any('dispute' in m.subject.lower() for m in mail.outbox))

    def test_validation(self):
        order = self.paid_order()
        with self.assertRaises(services.DisputeError):
            self.open(order, description='too short')
        with self.assertRaises(services.DisputeError):
            self.open(order, category='digital_file')
        with self.assertRaises(services.DisputeError):
            self.open(order, desired_resolution='partial_refund', requested_amount=Decimal('99999'))
        other_buyer = User.objects.create_user('other', 'o@x.com', 'pw')
        with self.assertRaises(services.DisputeError):
            services.open_dispute(order, other_buyer, category='damaged', description=LONG_TEXT, desired_resolution='full_refund')

    def test_dispute_opened_before_delivery_holds_the_payout_when_it_is_created(self):
        order = make_order(self.seller, self.buyer, status='shipped', placed_days_ago=10, payment_status='escrow_held')
        d = self.open(order, category='not_received')
        self.assertFalse(d.payout_held)  # no payout exists yet
        order.refresh_from_db()
        payout = payout_services.ensure_payout(order)  # what confirm_delivery does
        payout.refresh_from_db()
        d.refresh_from_db()
        self.assertEqual(payout.status, 'on_hold')
        self.assertTrue(d.payout_held)


class ConversationTests(DisputeTestBase):
    def setUp(self):
        super().setUp()
        self.order = self.paid_order()
        self.d = self.open(self.order)

    def test_seller_reply_moves_to_seller_responded(self):
        with self.captureOnCommitCallbacks(execute=True):
            services.post_message(self.d.pk, self.seller, 'Sorry about that, can you send a photo?')
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'seller_responded')
        self.assertTrue(Notification.objects.filter(user=self.buyer, title__icontains='New reply').exists())

    def test_buyer_cannot_escalate_before_seller_window_ends(self):
        with self.assertRaises(services.DisputeError):
            services.escalate(self.d.pk, self.buyer)

    def test_buyer_can_escalate_after_seller_replies(self):
        services.post_message(self.d.pk, self.seller, 'Looking into it.')
        services.escalate(self.d.pk, self.buyer, 'Seller is stalling')
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'escalated')
        self.assertIsNotNone(self.d.escalated_at)

    def test_buyer_can_escalate_after_deadline(self):
        Dispute.objects.filter(pk=self.d.pk).update(seller_response_due=timezone.now() - timedelta(hours=1))
        services.escalate(self.d.pk, self.buyer)
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'escalated')

    def test_only_buyer_escalates_and_only_seller_agrees(self):
        services.post_message(self.d.pk, self.seller, 'ok')
        with self.assertRaises(services.DisputeError):
            services.escalate(self.d.pk, self.seller)
        with self.assertRaises(services.DisputeError):
            services.seller_agree(self.d.pk, self.buyer)

    def test_seller_agree_goes_straight_to_staff(self):
        services.seller_agree(self.d.pk, self.seller, 'You are right, I am sorry.')
        self.d.refresh_from_db()
        self.assertEqual((self.d.status, self.d.seller_agreed), ('escalated', True))
        # agreeing does NOT move money - only staff_resolve does
        self.assertFalse(WalletTransaction.objects.filter(source='refund').exists())

    def test_strangers_cannot_post(self):
        with self.assertRaises(services.DisputeError):
            services.post_message(self.d.pk, self.stranger, 'hello')

    def test_overdue_command_escalates_silent_sellers_only(self):
        quiet = self.d
        Dispute.objects.filter(pk=quiet.pk).update(seller_response_due=timezone.now() - timedelta(hours=1))
        other_order = self.paid_order()
        fresh = self.open(other_order)
        replied_order = self.paid_order()
        replied = self.open(replied_order)
        services.post_message(replied.pk, self.seller, 'on it')
        Dispute.objects.filter(pk=replied.pk).update(seller_response_due=timezone.now() - timedelta(hours=1))
        call_command('escalate_overdue_disputes', stdout=io.StringIO())
        quiet.refresh_from_db(); fresh.refresh_from_db(); replied.refresh_from_db()
        self.assertEqual((quiet.status, quiet.auto_escalated), ('escalated', True))
        self.assertEqual(fresh.status, 'open')
        self.assertEqual(replied.status, 'seller_responded')

    def test_withdraw_releases_only_the_hold_it_placed(self):
        self.assertEqual(Payout.objects.get(order=self.order).status, 'on_hold')
        services.withdraw(self.d.pk, self.buyer)
        self.assertEqual(Payout.objects.get(order=self.order).status, 'pending')
        # a hold staff placed themselves is left alone
        order2 = self.paid_order()
        payout2 = Payout.objects.get(order=order2)
        payout_services.put_on_hold(payout2.pk, note='manual review')
        d2 = self.open(order2)
        self.assertFalse(d2.payout_held)
        services.withdraw(d2.pk, self.buyer)
        self.assertEqual(Payout.objects.get(order=order2).status, 'on_hold')

    def test_no_replies_after_close(self):
        services.withdraw(self.d.pk, self.buyer)
        with self.assertRaises(services.DisputeError):
            services.post_message(self.d.pk, self.seller, 'late')


class StaffResolveTests(DisputeTestBase):
    def setUp(self):
        super().setUp()
        self.order = self.paid_order(price=500)  # buyer paid 540
        self.d = self.open(self.order)

    def resolve(self, **kw):
        defaults = dict(outcome='refund', note='Pages are missing - refunding.', refund_amount=Decimal('500'), destination='wallet')
        defaults.update(kw)
        return services.staff_resolve(self.d.pk, self.staff, **defaults)

    def test_full_refund_to_wallet_reverses_the_sale(self):
        rr = ReturnRequest.objects.create(order=self.order, reason='damaged')
        d = self.resolve()
        self.assertEqual((d.status, d.outcome, d.refund_amount), ('resolved', 'refund', Decimal('500.00')))
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('500.00'))
        self.assertEqual(WalletTransaction.objects.filter(source='refund', order=self.order).count(), 1)
        self.assertEqual(Payment.objects.get(order=self.order).status, 'refunded')
        self.assertEqual(Payout.objects.get(order=self.order).status, 'cancelled')
        rr.refresh_from_db()
        self.assertEqual(rr.status, 'refunded')
        self.assertTrue(d.refund_reference.startswith('wallet-txn-'))

    def test_cannot_refund_more_than_buyer_paid(self):
        with self.assertRaises(services.DisputeError):
            self.resolve(refund_amount=Decimal('540.01'))
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'open')
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('0.00'))

    def test_partial_refund_deducts_from_pending_payout(self):
        before = Payout.objects.get(order=self.order).net_amount  # 450
        d = self.resolve(refund_amount=Decimal('100'), deduct_from_seller=True)
        payout = Payout.objects.get(order=self.order)
        self.assertEqual(payout.net_amount, before - Decimal('100'))
        self.assertEqual(payout.status, 'pending')  # hold released again
        self.assertIn('Partial refund', payout.note)
        self.assertEqual(Payment.objects.get(order=self.order).status, 'released')  # sale stands
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('100.00'))
        self.assertTrue(d.deducted_from_seller)

    def test_partial_refund_borne_by_platform_leaves_payout_alone(self):
        before = Payout.objects.get(order=self.order).net_amount
        self.resolve(refund_amount=Decimal('100'), deduct_from_seller=False)
        self.assertEqual(Payout.objects.get(order=self.order).net_amount, before)

    def test_partial_refund_after_payout_is_flagged_for_recovery(self):
        services.withdraw(self.d.pk, self.buyer)  # free the order, then re-dispute after the seller was paid
        payout = Payout.objects.get(order=self.order)
        payout_services.mark_paid([payout.pk], utr='UTR123456', actor=self.staff) if payout_services.get_upi_id(self.seller) else None
        Payout.objects.filter(pk=payout.pk).update(status='paid', paid_at=timezone.now(), utr='UTR123456')
        d = self.open(self.order)
        services.staff_resolve(d.pk, self.staff, outcome='refund', note='Partial.', refund_amount=Decimal('100'), destination='wallet')
        self.assertTrue(Payout.objects.get(pk=payout.pk).refunded_after_payout)

    def test_no_double_refund_via_dispute_then_return_admin_path(self):
        self.resolve()
        self.assertTrue(services.order_already_refunded(Order.objects.get(pk=self.order.pk)))
        with self.assertRaises(services.DisputeError):
            services.staff_resolve(self.d.pk, self.staff, outcome='refund', note='again', refund_amount=Decimal('10'))

    def test_partial_dispute_refund_blocks_a_second_refund_from_returns(self):
        self.resolve(refund_amount=Decimal('100'))
        self.assertTrue(services.order_already_refunded(Order.objects.get(pk=self.order.pk)))

    def test_unpaid_cod_order_cannot_be_refunded_but_can_be_declined(self):
        order = make_order(self.seller, self.buyer, status='shipped', placed_days_ago=10, payment_status='pending', method='cod')
        d = self.open(order, category='not_received')
        with self.assertRaises(services.DisputeError):
            services.staff_resolve(d.pk, self.staff, outcome='refund', note='x', refund_amount=Decimal('50'))
        services.staff_resolve(d.pk, self.staff, outcome='rejected', note='Courier shows it was never collected.')
        d.refresh_from_db()
        self.assertEqual((d.status, d.outcome), ('resolved', 'rejected'))
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('0.00'))

    def test_decline_releases_the_hold_and_pays_nothing(self):
        self.assertEqual(Payout.objects.get(order=self.order).status, 'on_hold')
        self.resolve(outcome='rejected', refund_amount=None, note='Photos show the book matches the listing.')
        self.assertEqual(Payout.objects.get(order=self.order).status, 'pending')
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('0.00'))

    def test_replacement_closes_without_money(self):
        d = self.resolve(outcome='replacement', refund_amount=None, note='Seller will resend.')
        self.assertEqual(d.outcome, 'replacement')
        self.assertIsNone(d.refund_amount)
        self.assertEqual(Payout.objects.get(order=self.order).status, 'pending')

    def test_note_is_required(self):
        with self.assertRaises(services.DisputeError):
            self.resolve(note='   ')

    def test_razorpay_refund_uses_the_gateway_with_the_right_amount(self):
        order = self.paid_order(razorpay_id='pay_ABC123')
        d = self.open(order)
        with mock.patch('disputes.services.razorpay_gateway.refund_payment', return_value={'id': 'rfnd_1'}) as gw:
            services.staff_resolve(d.pk, self.staff, outcome='refund', note='ok', refund_amount=Decimal('500'), destination='original')
        gw.assert_called_once()
        self.assertEqual(gw.call_args.args[0], 'pay_ABC123')
        self.assertEqual(gw.call_args.kwargs['amount_rupees'], Decimal('500.00'))
        d.refresh_from_db()
        self.assertEqual(d.refund_reference, 'rfnd_1')
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('0.00'))  # not double-paid to the wallet

    def test_gateway_failure_changes_nothing(self):
        order = self.paid_order(razorpay_id='pay_FAIL')
        d = self.open(order)
        with mock.patch('disputes.services.razorpay_gateway.refund_payment', side_effect=RuntimeError('boom')):
            with self.assertRaises(services.DisputeError):
                services.staff_resolve(d.pk, self.staff, outcome='refund', note='ok', refund_amount=Decimal('500'), destination='original')
        d.refresh_from_db()
        self.assertEqual(d.status, 'open')
        self.assertEqual(Payment.objects.get(order=order).status, 'released')
        self.assertEqual(Payout.objects.get(order=order).status, 'on_hold')

    def test_original_destination_needs_an_online_payment(self):
        with self.assertRaises(services.DisputeError):
            self.resolve(destination='original')  # self.order has no razorpay id

    def test_cannot_resolve_twice(self):
        self.resolve()
        with self.assertRaises(services.DisputeError):
            self.resolve()
        self.assertEqual(WalletTransaction.objects.filter(source='refund').count(), 1)

    def test_both_parties_are_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.resolve()
        for user in (self.buyer, self.seller):
            self.assertTrue(Notification.objects.filter(user=user, title__icontains='resolved').exists(), user.username)


class ViewTests(DisputeTestBase):
    def setUp(self):
        super().setUp()
        self.order = self.paid_order()

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password='pw'))

    def test_order_page_offers_the_button_to_the_buyer_only(self):
        url = reverse('orders:detail', args=[self.order.order_id])
        self.login(self.buyer)
        self.assertContains(self.client.get(url), reverse('disputes:open', args=[self.order.order_id]))
        self.login(self.seller)
        self.assertNotContains(self.client.get(url), reverse('disputes:open', args=[self.order.order_id]))

    def test_buyer_opens_dispute_through_the_form_with_a_photo(self):
        self.login(self.buyer)
        url = reverse('disputes:open', args=[self.order.order_id])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.post(url, {
            'category': 'missing_pages', 'description': LONG_TEXT, 'desired_resolution': 'full_refund',
            'evidence': [png_upload('a.png'), png_upload('b.png')],
        })
        d = Dispute.objects.get()
        self.assertRedirects(resp, reverse('disputes:detail', args=[d.pk]))
        self.assertEqual(d.evidence.count(), 2)

    def test_non_image_upload_is_rejected(self):
        self.login(self.buyer)
        resp = self.client.post(reverse('disputes:open', args=[self.order.order_id]), {
            'category': 'damaged', 'description': LONG_TEXT, 'desired_resolution': 'full_refund',
            'evidence': SimpleUploadedFile('evil.png', b'<?php echo 1; ?>', content_type='image/png'),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Dispute.objects.exists())

    def test_partial_refund_requires_an_amount(self):
        self.login(self.buyer)
        resp = self.client.post(reverse('disputes:open', args=[self.order.order_id]), {
            'category': 'damaged', 'description': LONG_TEXT, 'desired_resolution': 'partial_refund',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Dispute.objects.exists())

    def test_only_the_buyer_can_open_it(self):
        for user in (self.seller, self.stranger):
            self.login(user)
            self.assertEqual(self.client.get(reverse('disputes:open', args=[self.order.order_id])).status_code, 404)

    def test_detail_is_private_to_the_two_parties(self):
        d = self.open(self.order)
        url = reverse('disputes:detail', args=[d.pk])
        self.login(self.stranger)
        self.assertEqual(self.client.get(url).status_code, 404)
        for user in (self.buyer, self.seller):
            self.login(user)
            self.assertContains(self.client.get(url), d.reference)
        self.login(self.staff)
        self.assertRedirects(self.client.get(url), reverse('disputes:staff_detail', args=[d.pk]))

    def test_evidence_is_served_to_participants_and_staff_only(self):
        d = self.open(self.order, files=[png_upload()])
        ev = d.evidence.get()
        url = reverse('disputes:evidence', args=[ev.pk])
        for user, code in ((self.buyer, 200), (self.seller, 200), (self.staff, 200), (self.stranger, 404)):
            self.login(user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, code, user.username)
            if code == 200:
                self.assertEqual(resp['Content-Type'], 'image/png')
                self.assertEqual(resp['X-Content-Type-Options'], 'nosniff')
                b''.join(resp.streaming_content)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)  # login required

    def test_internal_notes_never_reach_the_parties(self):
        d = self.open(self.order)
        services.staff_note(d.pk, self.staff, 'Buyer has 3 prior disputes - be careful.')
        for user in (self.buyer, self.seller):
            self.login(user)
            self.assertNotContains(self.client.get(reverse('disputes:detail', args=[d.pk])), 'prior disputes')
        self.login(self.staff)
        self.assertContains(self.client.get(reverse('disputes:staff_detail', args=[d.pk])), 'prior disputes')

    def test_message_and_escalate_flow_over_http(self):
        d = self.open(self.order)
        self.login(self.seller)
        self.client.post(reverse('disputes:message', args=[d.pk]), {'body': 'Please send a photo of the pages.'})
        self.login(self.buyer)
        self.client.post(reverse('disputes:escalate', args=[d.pk]), {'reason': 'Need staff'})
        d.refresh_from_db()
        self.assertEqual(d.status, 'escalated')

    def test_staff_pages_are_staff_only(self):
        d = self.open(self.order)
        for name, args in (('staff_queue', []), ('staff_detail', [d.pk])):
            self.login(self.buyer)
            self.assertEqual(self.client.get(reverse(f'disputes:{name}', args=args)).status_code, 302)
        self.login(self.buyer)
        self.client.post(reverse('disputes:staff_resolve', args=[d.pk]), {
            'outcome': 'refund', 'note': 'x', 'refund_amount': '500', 'destination': 'wallet',
        })
        d.refresh_from_db()
        self.assertEqual(d.status, 'open')
        self.assertEqual(get_wallet(self.buyer).balance, Decimal('0.00'))

    def test_staff_resolves_over_http_and_it_is_audit_logged(self):
        d = self.open(self.order)
        services.seller_agree(d.pk, self.seller)
        self.login(self.staff)
        self.assertContains(self.client.get(reverse('disputes:staff_queue')), d.reference)
        self.assertEqual(self.client.get(reverse('disputes:staff_detail', args=[d.pk])).status_code, 200)
        self.client.post(reverse('disputes:staff_resolve', args=[d.pk]), {
            'outcome': 'refund', 'note': 'Agreed by seller.', 'refund_amount': '500', 'destination': 'wallet',
        })
        d.refresh_from_db()
        self.assertEqual(d.status, 'resolved')
        log = AuditLog.objects.get(action='resolve_dispute')
        self.assertEqual((log.actor, log.target_id), (self.staff, d.pk))

    def test_dashboard_and_lists_render(self):
        d = self.open(self.order)
        self.login(self.buyer)
        self.assertContains(self.client.get(reverse('disputes:mine')), d.reference)
        self.login(self.seller)
        self.assertContains(self.client.get(reverse('disputes:mine')), d.reference)
        self.login(self.staff)
        self.assertEqual(self.client.get(reverse('adminpanel:dashboard')).status_code, 200)
