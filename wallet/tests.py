"""
Tests for wallet/services.py - the module every balance change goes through.

It had no test file at all, which is how the replayable-cashback bug in
orders.views.confirm_delivery went unnoticed: the ledger invariants below
all held, the caller just ran the credit more than once.
"""
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from books.models import Book, Category
from orders.models import Order

from .models import WalletTransaction
from .services import (
    CASHBACK_PERCENT, REFERRAL_REWARD, InsufficientBalance, InvalidAmount,
    award_delivery_cashback, cashback_for_amount, credit, debit, get_wallet,
    reward_referral_if_eligible, referral_blocked_reason, signup_bonus_allowed,
    REFERRAL_MAX_REWARDS_PER_DAY, REFERRAL_MAX_SIGNUP_BONUSES_PER_DAY,
)


def make_order(buyer, seller, price=1000, status='delivered'):
    category, _ = Category.objects.get_or_create(name='Test Category')
    book = Book.objects.create(seller=seller, category=category, title='Some Book', price=price)
    return Order.objects.create(
        buyer=buyer, book=book, final_price=price, delivery_charge=40, payment_method='upi',
        status=status, delivery_address='1 MG Road', delivery_city='Kanpur',
        delivery_pincode='208001',
    )


class LedgerIntegrityTests(TestCase):
    """The running balance and the transaction ledger must never disagree -
    that's the whole reason callers go through credit()/debit() instead of
    touching Wallet.balance."""

    def setUp(self):
        self.user = User.objects.create_user(username='walletuser', password='pw')

    def test_balance_after_matches_running_balance(self):
        credit(self.user, Decimal('100'), 'cashback')
        credit(self.user, Decimal('50'), 'referral')
        debit(self.user, Decimal('30'), 'payment')

        wallet = get_wallet(self.user)
        self.assertEqual(wallet.balance, Decimal('120.00'))
        latest = WalletTransaction.objects.filter(wallet=wallet).order_by('-id').first()
        self.assertEqual(latest.balance_after, wallet.balance)

    def test_debit_beyond_balance_is_refused(self):
        credit(self.user, Decimal('10'), 'cashback')

        with self.assertRaises(InsufficientBalance):
            debit(self.user, Decimal('10.01'), 'payment')

        self.assertEqual(get_wallet(self.user).balance, Decimal('10.00'))
        self.assertEqual(WalletTransaction.objects.filter(txn_type='debit').count(), 0)

    def test_non_positive_amounts_are_rejected(self):
        # Used to return None silently, which let a Rs 0 order look "paid".
        with self.assertRaises(InvalidAmount):
            credit(self.user, Decimal('0'), 'cashback')
        with self.assertRaises(InvalidAmount):
            credit(self.user, Decimal('-5'), 'cashback')
        with self.assertRaises(InvalidAmount):
            debit(self.user, Decimal('0'), 'payment')
        with self.assertRaises(InvalidAmount):
            debit(self.user, Decimal('-5'), 'payment')
        self.assertEqual(WalletTransaction.objects.count(), 0)

    def test_amounts_are_rounded_to_paise(self):
        credit(self.user, Decimal('10.005'), 'cashback')

        self.assertEqual(get_wallet(self.user).balance, Decimal('10.01'))


class CashbackTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='cb_seller', password='pw')
        self.buyer = User.objects.create_user(username='cb_buyer', password='pw')

    def test_cashback_is_the_configured_percentage(self):
        self.assertEqual(
            cashback_for_amount(Decimal('1000')),
            Decimal('1000') * CASHBACK_PERCENT / Decimal('100'),
        )

    def test_award_credits_the_buyer(self):
        order = make_order(self.buyer, self.seller, price=1000)

        award_delivery_cashback(order)

        self.assertEqual(get_wallet(self.buyer).balance, Decimal('20.00'))


class ReferralRewardTests(TestCase):
    """A referrer is paid once, on the referred friend's FIRST delivered
    order - not on signup, and not again on later orders."""

    def setUp(self):
        self.seller = User.objects.create_user(username='ref_seller', password='pw')
        self.referrer = User.objects.create_user(username='referrer', password='pw')
        self.friend = User.objects.create_user(username='friend', password='pw')
        self.friend.referred_by = self.referrer
        self.friend.email_verified = True
        self.friend.save(update_fields=['referred_by', 'email_verified'])
        self.referrer.email_verified = True
        self.referrer.save(update_fields=['email_verified'])

    def test_first_delivered_order_pays_the_referrer(self):
        order = make_order(self.friend, self.seller)

        reward_referral_if_eligible(order)

        self.assertEqual(get_wallet(self.referrer).balance, REFERRAL_REWARD)

    def test_referrer_is_not_paid_twice_for_the_same_friend(self):
        first = make_order(self.friend, self.seller)
        reward_referral_if_eligible(first)

        second = make_order(self.friend, self.seller)
        reward_referral_if_eligible(second)

        self.assertEqual(get_wallet(self.referrer).balance, REFERRAL_REWARD)

    def test_calling_twice_on_one_order_pays_once(self):
        order = make_order(self.friend, self.seller)

        reward_referral_if_eligible(order)
        reward_referral_if_eligible(order)

        self.assertEqual(get_wallet(self.referrer).balance, REFERRAL_REWARD)

    def test_unreferred_buyer_pays_nobody(self):
        stranger = User.objects.create_user(username='stranger', password='pw')
        order = make_order(stranger, self.seller)

        self.assertIsNone(reward_referral_if_eligible(order))
        self.assertEqual(WalletTransaction.objects.filter(source='referral').count(), 0)


class ReferralAbuseLimitTests(TestCase):
    """The referral reward is only paid for a real, sizeable, non-duplicate purchase, and is capped."""

    def setUp(self):
        self.seller = User.objects.create_user(username='abuse_seller', password='pw')
        self.referrer = User.objects.create_user(username='abuse_ref', password='pw', email_verified=True)

    def _friend(self, name, **extra):
        friend = User.objects.create_user(username=name, password='pw', email_verified=True, **extra)
        friend.referred_by = self.referrer
        friend.save(update_fields=['referred_by'])
        return friend

    def test_small_orders_do_not_earn_a_reward_but_a_later_bigger_one_does(self):
        friend = self._friend('small')
        self.assertIsNone(reward_referral_if_eligible(make_order(friend, self.seller, price=50)))
        self.assertEqual(get_wallet(self.referrer).balance, 0)
        friend.refresh_from_db()
        self.assertFalse(friend.referral_bonus_paid)
        reward_referral_if_eligible(make_order(friend, self.seller, price=500))
        self.assertEqual(get_wallet(self.referrer).balance, REFERRAL_REWARD)

    def test_unverified_email_blocks_the_reward(self):
        friend = self._friend('unverified')
        User.objects.filter(pk=friend.pk).update(email_verified=False)
        friend.refresh_from_db()
        self.assertIsNone(reward_referral_if_eligible(make_order(friend, self.seller)))
        User.objects.filter(pk=self.referrer.pk).update(email_verified=False)
        User.objects.filter(pk=friend.pk).update(email_verified=True)
        friend.refresh_from_db()
        self.assertIsNone(reward_referral_if_eligible(make_order(friend, self.seller)))

    def test_linked_account_with_same_phone_is_blocked(self):
        User.objects.filter(pk=self.referrer.pk).update(phone='9876543210')
        friend = self._friend('twin', phone='9876543210')
        self.assertIsNone(reward_referral_if_eligible(make_order(friend, self.seller)))
        self.assertEqual(get_wallet(self.referrer).balance, 0)

    def test_self_referral_is_blocked(self):
        self.referrer.referred_by = self.referrer
        self.referrer.save(update_fields=['referred_by'])
        self.assertEqual(referral_blocked_reason(self.referrer, self.referrer), 'self-referral')

    def test_daily_cap(self):
        paid = 0
        for i in range(5):
            friend = self._friend(f'cap{i}')
            if reward_referral_if_eligible(make_order(friend, self.seller)):
                paid += 1
        self.assertEqual(paid, REFERRAL_MAX_REWARDS_PER_DAY)
        self.assertEqual(get_wallet(self.referrer).balance, REFERRAL_REWARD * REFERRAL_MAX_REWARDS_PER_DAY)

    def test_signup_bonus_is_capped_per_referrer_per_day(self):
        results = []
        for i in range(REFERRAL_MAX_SIGNUP_BONUSES_PER_DAY + 2):
            friend = self._friend(f'welcome{i}')
            allowed = signup_bonus_allowed(friend, self.referrer)
            results.append(allowed)
            if allowed:
                credit(friend, Decimal('50'), 'referral', idempotency_key=f'signup-bonus:{friend.pk}')
        self.assertEqual(results.count(True), REFERRAL_MAX_SIGNUP_BONUSES_PER_DAY)
        self.assertEqual(results[-2:], [False, False])

    def test_signup_bonus_blocked_for_linked_accounts(self):
        User.objects.filter(pk=self.referrer.pk).update(phone='9000000001')
        friend = self._friend('welcome_twin', phone='9000000001')
        self.assertFalse(signup_bonus_allowed(friend, self.referrer))
