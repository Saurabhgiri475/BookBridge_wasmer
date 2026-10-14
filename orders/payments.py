"""
Real Razorpay integration.

Requires RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (test or live) from your
Razorpay dashboard (https://dashboard.razorpay.com/app/keys), set as
environment variables - see .env.example. Everything here is a thin,
honest wrapper around the official `razorpay` Python SDK; no behaviour is
faked. In test mode Razorpay gives you working fake card/UPI credentials
(https://razorpay.com/docs/payments/payments/test-card-upi-details/) so
you can walk the full flow without moving real money.

Escrow note: Razorpay itself just captures/refunds a payment - it doesn't
hold marketplace escrow for you (that needs a separate product like
Razorpay Route/Escrow accounts, which requires a business KYC review).
This app models escrow on top of a real captured payment: `Payment.status`
tracks 'escrow_held' -> 'released' locally, and the actual money sits in
your Razorpay account balance until you release it to the seller by your
own payout process (bank transfer, RazorpayX payout, etc).
"""
import hashlib
import hmac

import razorpay
from django.conf import settings


class RazorpayConfigError(Exception):
    """Raised when RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET aren't configured."""


def get_client():
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise RazorpayConfigError(
            'RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. '
            'Add them to your environment (see .env.example) to accept real payments.'
        )
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_order(amount_rupees, receipt, notes=None):
    """
    Creates a Razorpay Order (a server-side intent to collect payment).
    Amount must be sent in paise (smallest currency unit) per Razorpay's API.
    Returns the Razorpay order dict, e.g. {'id': 'order_xxx', 'amount': ..., ...}.
    """
    client = get_client()
    amount_paise = int(round(float(amount_rupees) * 100))
    return client.order.create({
        'amount': amount_paise,
        'currency': 'INR',
        'receipt': receipt,
        'notes': notes or {},
        'payment_capture': 1,  # auto-capture on successful authorization
    })


def verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
    """
    Verifies the signature Razorpay Checkout hands back on the client-side
    success callback. Raises razorpay.errors.SignatureVerificationError if
    it doesn't match (i.e. the callback was tampered with / forged).
    """
    client = get_client()
    client.utility.verify_payment_signature({
        'razorpay_order_id': razorpay_order_id,
        'razorpay_payment_id': razorpay_payment_id,
        'razorpay_signature': razorpay_signature,
    })


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verifies the X-Razorpay-Signature header on incoming webhook events
    using RAZORPAY_WEBHOOK_SECRET (set separately from the API keys, in the
    Razorpay dashboard's Webhooks section). Returns True/False rather than
    raising, since a webhook handler should just 400 on mismatch.
    """
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode('utf-8'), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header or '')


def refund_payment(razorpay_payment_id, amount_rupees=None, notes=None):
    """
    Issues a refund for a captured payment. If amount_rupees is omitted,
    Razorpay refunds the full original amount. Returns the refund dict.
    """
    client = get_client()
    payload = {'notes': notes or {}}
    if amount_rupees is not None:
        payload['amount'] = int(round(float(amount_rupees) * 100))
    return client.payment.refund(razorpay_payment_id, payload)


def fetch_payment(razorpay_payment_id):
    client = get_client()
    return client.payment.fetch(razorpay_payment_id)


def amount_to_paise(amount_rupees):
    """Rupee Decimal/str/float -> integer paise, exactly as create_order() sends it."""
    return int(round(float(amount_rupees) * 100))


def check_payment_against_payment_row(rp_payment, payment):
    """
    Compares a Razorpay payment entity (from the API or a webhook payload)
    with our local Payment row. Returns (ok, reason). Never raises.

    A valid signature only proves Razorpay signed *some* (order, payment) pair.
    It does not prove that pair is the one we created for THIS BookBridge
    order, or that the buyer paid the right amount, so check all of it here.
    """
    if not payment.razorpay_order_id:
        return False, 'local payment has no Razorpay order id'
    if rp_payment.get('order_id') != payment.razorpay_order_id:
        return False, 'payment belongs to a different Razorpay order'
    if rp_payment.get('amount') != amount_to_paise(payment.amount):
        return False, 'amount mismatch'
    if (rp_payment.get('currency') or '').upper() != 'INR':
        return False, 'currency mismatch'
    return True, ''


def verify_payment_for_order(razorpay_payment_id, payment):
    """
    Server-side confirmation straight from Razorpay's API (not from anything
    the browser sent). Returns (state, reason) where state is:
      'captured'  - verified and money is captured: safe to finalize
      'pending'   - verified as ours but not captured yet (e.g. 'authorized');
                    the payment.captured webhook will finalize it
      'invalid'   - does not match this order / amount / currency, or failed
    """
    try:
        rp_payment = fetch_payment(razorpay_payment_id)
    except Exception as exc:  # network error, unknown payment id, bad keys...
        return 'invalid', f'could not fetch payment from Razorpay: {exc}'
    ok, reason = check_payment_against_payment_row(rp_payment, payment)
    if not ok:
        return 'invalid', reason
    status = rp_payment.get('status')
    if status == 'captured':
        return 'captured', ''
    if status == 'authorized':
        return 'pending', ''
    return 'invalid', f'unexpected payment status: {status}'

