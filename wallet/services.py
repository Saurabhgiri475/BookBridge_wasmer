"""
Wallet business logic.

Every balance change goes through `credit()` or `debit()` below - never
`Wallet.balance` directly - so the running balance and the transaction
ledger (`WalletTransaction`) can never drift apart. Both run inside a
`select_for_update()` lock so two concurrent requests (e.g. a delivery
confirmation and a checkout debit racing each other) can't both read a
stale balance and push it out of sync.

The reward rules (cashback %, referral amounts) are estimates/business
choices, not laws of physics - kept as constants here, in one place, the
same pattern as bookmarket/sustainability.py.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from .models import Wallet, WalletTransaction

# --- Reward rules --------------------------------------------------------
CASHBACK_PERCENT = Decimal('2.0')          # % of order value credited as cashback once delivery is confirmed
REFERRAL_SIGNUP_BONUS = Decimal('50.00')   # credited to a new user immediately, if they signed up via a referral link
REFERRAL_REWARD = Decimal('100.00')        # credited to the referrer once their referred friend's FIRST order is delivered

# --- Referral anti-abuse limits -------------------------------------------
REFERRAL_MIN_ORDER_VALUE = Decimal('200.00')   # the referred friend's order (book price) must be worth at least this
REFERRAL_MAX_REWARDS_PER_DAY = 3               # rewards one referrer can earn per rolling 24 hours...
REFERRAL_MAX_REWARDS_PER_MONTH = 10            # ...and per rolling 30 days
REFERRAL_MAX_SIGNUP_BONUSES_PER_DAY = 5        # welcome bonuses handed out through one referrer's link per 24 hours


class InvalidAmount(ValueError):
    """Raised by credit()/debit() for an amount that is zero or negative. Silently doing nothing
    here used to let a Rs 0 order look "paid" - a bad amount is always a bug or an attack."""


class InsufficientBalance(Exception):
    """Raised by debit() when the wallet doesn't have enough balance to cover the amount."""


def get_wallet(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def cashback_for_amount(order_amount):
    return (Decimal(order_amount) * CASHBACK_PERCENT / Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )


class IdempotencyConflict(Exception):
    """The same idempotency key was reused for a different wallet, direction or amount - a bug in the caller."""


def _already_done(wallet, key, txn_type, amount):
    """Returns the transaction a previous call with this key created (None if the key is new).
    Must run while the wallet row is locked, so two racing calls can't both see "new"."""
    if not key:
        return None
    existing = WalletTransaction.objects.filter(idempotency_key=key).select_related('wallet').first()
    if existing is None:
        return None
    if existing.wallet_id != wallet.pk or existing.txn_type != txn_type or existing.amount != amount:
        raise IdempotencyConflict(f'Idempotency key {key!r} was already used for a different transaction.')
    return existing


@transaction.atomic
def credit(user, amount, source, description='', order=None, idempotency_key=None):
    """Add money. With an idempotency_key the credit happens at most once: repeating the call
    (double click, retry, two workers) returns the original transaction and changes nothing."""
    amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise InvalidAmount(f'Wallet credit must be positive, got {amount}.')
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
    existing = _already_done(wallet, idempotency_key, 'credit', amount)
    if existing:
        return existing
    wallet.balance += amount
    wallet.save(update_fields=['balance', 'updated_at'])
    return WalletTransaction.objects.create(
        wallet=wallet, txn_type='credit', source=source, amount=amount,
        balance_after=wallet.balance, description=description, order=order,
        idempotency_key=idempotency_key or None,
    )


@transaction.atomic
def debit(user, amount, source, description='', order=None, idempotency_key=None):
    """Take money. With an idempotency_key the debit happens at most once (see credit())."""
    amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise InvalidAmount(f'Wallet debit must be positive, got {amount}.')
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
    existing = _already_done(wallet, idempotency_key, 'debit', amount)
    if existing:
        return existing
    if wallet.balance < amount:
        raise InsufficientBalance(f'Wallet balance ₹{wallet.balance} is less than ₹{amount}.')
    wallet.balance -= amount
    wallet.save(update_fields=['balance', 'updated_at'])
    return WalletTransaction.objects.create(
        wallet=wallet, txn_type='debit', source=source, amount=amount,
        balance_after=wallet.balance, description=description, order=order,
        idempotency_key=idempotency_key or None,
    )


def award_delivery_cashback(order):
    """Call once, right when an order transitions to 'delivered'."""
    amount = cashback_for_amount(order.final_price)
    if amount <= 0:
        return None
    return credit(
        order.buyer, amount, 'cashback', order=order,
        description=f'{CASHBACK_PERCENT}% cashback on order #{str(order.order_id)[:8]} ("{order.book.title}")',
        idempotency_key=f'cashback:{order.order_id}',  # order_id is a UUID: never reused, unlike a database pk
    )


def referral_blocked_reason(buyer, referrer, order=None):
    """Why a referral reward must NOT be paid for this buyer/referrer pair, or '' when it is fine.
    Pure checks (no writes), so it is easy to test and to reuse in an admin screen."""
    from datetime import timedelta
    from django.utils import timezone
    from accounts.fraud_detection import find_linked_accounts

    if buyer.pk == referrer.pk:
        return 'self-referral'
    if not (buyer.email_verified and referrer.email_verified):
        return 'both accounts need a verified email'
    if any(link['user'].pk == referrer.pk for link in find_linked_accounts(buyer)):
        return 'accounts look linked (same phone number)'
    if order is not None and order.final_price < REFERRAL_MIN_ORDER_VALUE:
        return f'order is below the \u20b9{REFERRAL_MIN_ORDER_VALUE} minimum'
    now = timezone.now()
    earned = WalletTransaction.objects.filter(
        wallet__user=referrer, idempotency_key__startswith='referral-reward:',
    )
    if earned.filter(created_at__gte=now - timedelta(days=1)).count() >= REFERRAL_MAX_REWARDS_PER_DAY:
        return 'daily referral reward limit reached'
    if earned.filter(created_at__gte=now - timedelta(days=30)).count() >= REFERRAL_MAX_REWARDS_PER_MONTH:
        return 'monthly referral reward limit reached'
    return ''


def signup_bonus_allowed(new_user, referrer):
    """Welcome-bonus gate used right after a referred signup is verified: no self-referral or
    linked accounts, and a cap on how many bonuses one referrer's link can hand out per day."""
    from datetime import timedelta
    from django.utils import timezone
    from accounts.fraud_detection import find_linked_accounts

    if new_user.pk == referrer.pk:
        return False
    if any(link['user'].pk == referrer.pk for link in find_linked_accounts(new_user)):
        return False
    recent = WalletTransaction.objects.filter(
        idempotency_key__startswith='signup-bonus:',
        wallet__user__referred_by=referrer,
        created_at__gte=timezone.now() - timedelta(days=1),
    ).count()
    return recent < REFERRAL_MAX_SIGNUP_BONUSES_PER_DAY


def reward_referral_if_eligible(order):
    """
    Call once, right when an order transitions to 'delivered'. If the buyer was referred by someone
    and this is the buyer's first order worth at least REFERRAL_MIN_ORDER_VALUE, the referrer gets
    rewarded now - tied to a real completed purchase rather than just a signup. `referral_bonus_paid`
    makes sure a referrer is paid once per referred friend, however many orders follow.
    Abuse limits (verified emails, no self/linked accounts, daily/monthly caps) live in
    referral_blocked_reason(); a blocked reward is simply not paid, and nothing is marked as paid.
    """
    import logging
    buyer = order.buyer
    referrer = buyer.referred_by
    if not referrer or buyer.referral_bonus_paid:
        return None

    reason = referral_blocked_reason(buyer, referrer, order)
    if reason:
        logging.getLogger(__name__).info('Referral reward for %s -> %s not paid: %s', referrer.pk, buyer.pk, reason)
        return None

    buyer.referral_bonus_paid = True
    buyer.save(update_fields=['referral_bonus_paid'])
    return credit(
        referrer, REFERRAL_REWARD, 'referral', order=order,
        description=f"Referral reward - {buyer.username}'s first order was delivered",
        idempotency_key=f'referral-reward:{order.order_id}',
    )
