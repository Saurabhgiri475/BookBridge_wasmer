import hmac
import json
import logging

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

import razorpay

from books.models import Book, MIN_PRICE, RENTAL_DURATION_PERCENTAGES
from bookmarket.security import rate_limit
from . import payments as razorpay_gateway
from . import shiprocket
from .invoice import render_invoice_pdf
from .models import Order, Offer, Payment, Shipment, Review, ReturnRequest, MAX_DELIVERY_OTP_ATTEMPTS
from .forms import CheckoutForm, OfferForm, ReviewForm, ReturnRequestForm
from .utils import generate_otp
from wallet.services import get_wallet, credit as wallet_credit, debit as wallet_debit, InsufficientBalance
from wallet.services import award_delivery_cashback, reward_referral_if_eligible
from payouts.services import ensure_payout
from disputes.services import blocked_reason as dispute_blocked_reason
from coupons.services import validate_coupon, redeem_coupon, CouponError
from bookmarket.notify import (
    notify_new_offer, notify_offer_response, notify_order_placed, notify_delivery_otp,
    notify_order_status_update, notify_delivery_confirmed, notify_return_submitted, send_email,
    notify_digital_delivery,
)

logger = logging.getLogger(__name__)


@login_required
def make_offer(request, book_pk):
    book = get_object_or_404(Book, pk=book_pk, status='available')
    if request.method == 'POST':
        form = OfferForm(request.POST)
        if form.is_valid():
            offer = form.save(commit=False)
            offer.book = book
            offer.buyer = request.user
            offer.made_by = 'buyer'
            offer.save()
            messages.success(request, _('Your offer of ₹%(amount)s has been sent to the seller.') % {'amount': offer.amount})
            notify_new_offer(offer)
            return redirect('books:detail', pk=book.pk)
    else:
        form = OfferForm(initial={'amount': book.price})
    return render(request, 'orders/make_offer.html', {'form': form, 'book': book})


@login_required
@require_POST
def respond_offer(request, offer_pk):
    offer = get_object_or_404(Offer, pk=offer_pk, book__seller=request.user)
    action = request.POST.get('action')
    if action == 'accept':
        offer.status = 'accepted'
        offer.save()
        offer.book.status = 'reserved'
        offer.book.save(update_fields=['status'])
        messages.success(request, _('Offer accepted. The buyer can now check out at the agreed price.'))
        notify_offer_response(offer)
    elif action == 'reject':
        offer.status = 'rejected'
        offer.save()
        messages.info(request, _('Offer rejected.'))
        notify_offer_response(offer)
    elif action == 'counter':
        try:
            counter_amount = Decimal(request.POST.get('counter_amount', ''))
            if counter_amount < MIN_PRICE or counter_amount > Decimal('999999.99'):
                raise InvalidOperation
        except InvalidOperation:
            messages.error(request, _('Enter a valid counter-offer amount.'))
            return redirect('books:detail', pk=offer.book.pk)
        counter = Offer.objects.create(
            book=offer.book, buyer=offer.buyer, amount=counter_amount,
            made_by='seller', status='pending'
        )
        offer.status = 'countered'
        offer.save()
        messages.info(request, _('Counter-offer of ₹%(counter_amount)s sent to buyer.') % {'counter_amount': counter_amount})
        send_email(
            offer.buyer.email, f'Counter-offer on "{offer.book.title}"',
            f'The seller countered your offer on "{offer.book.title}" with ₹{counter_amount}. '
            f'Log in to accept, reject, or counter back.',
        )
    return redirect('books:detail', pk=offer.book.pk)


def _reserved_for_other_buyer(book, user):
    """True if `book` is reserved by an accepted offer belonging to someone else."""
    if book.status != 'reserved':
        return False
    return not Offer.objects.filter(book=book, buyer=user, status='accepted').exists()


def _resolve_final_price(book, user):
    """The price the buyer actually pays: their accepted offer if there is one, else the listed price."""
    accepted_offer = Offer.objects.filter(book=book, buyer=user, status='accepted').first()
    return (accepted_offer.amount if accepted_offer else book.price), accepted_offer


def _resolve_rent_days(book, request):
    """
    Reads rent_days from the query string (link from book_detail's rent
    picker) or POST (hidden field carried through the checkout form).
    Returns None for anything invalid, so callers can treat that the same
    as "buying, not renting" instead of erroring.
    """
    if not book.can_be_rented:
        return None
    raw = request.POST.get('rent_days') or request.GET.get('rent_days')
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    return days if days in RENTAL_DURATION_PERCENTAGES else None


def _mark_book_taken(book, order):
    """
    Flip the listing out of the marketplace once an order for it is confirmed
    - 'rented' vs 'sold'. Returns False if someone else got there first.

    The availability re-check and the status flip happen together under a row
    lock. checkout() used to re-check with a plain refresh_from_db(), which is
    a read: two buyers submitting the form in the same moment could both pass
    it and both end up with an order for the same physical copy. Claiming the
    row is what actually makes one of them lose.
    """
    taken_status = 'rented' if order.order_type == 'rental' else 'sold'
    # Rentals are never sold off the back of an accepted offer, so a
    # 'reserved' listing isn't rentable - matches the check in checkout().
    claimable = ('available',) if order.order_type == 'rental' else ('available', 'reserved')

    with transaction.atomic():
        locked = Book.objects.select_for_update().get(pk=book.pk)
        if locked.status not in claimable or _reserved_for_other_buyer(locked, order.buyer):
            return False
        if locked.is_payment_held_for_other(order.buyer):
            return False  # someone else is part-way through paying for it
        locked.status = taken_status
        locked.payment_reserved_by = None
        locked.payment_reserved_until = None
        locked.save(update_fields=['status', 'payment_reserved_by', 'payment_reserved_until'])

    book.status = taken_status
    return True


def _release_book_claim(book):
    """Undo _mark_book_taken() when the order it was claimed for falls through."""
    book.status = 'available'
    book.save(update_fields=['status'])


def _reserve_for_payment(book, user):
    """
    Hold the listing for `user` while they pay online (settings.PAYMENT_HOLD_MINUTES), so a second
    buyer cannot also start paying for the same physical copy. Under a row lock: returns False if
    the listing is no longer available to this buyer or another buyer holds it right now.
    The hold expires on its own; it is cleared when the book is taken or the payment fails.
    """
    with transaction.atomic():
        locked = Book.objects.select_for_update().get(pk=book.pk)
        if locked.status not in ('available', 'reserved') or _reserved_for_other_buyer(locked, user):
            return False
        if locked.is_payment_held_for_other(user):
            return False
        locked.payment_reserved_by = user
        locked.payment_reserved_until = timezone.now() + timedelta(minutes=settings.PAYMENT_HOLD_MINUTES)
        locked.save(update_fields=['payment_reserved_by', 'payment_reserved_until'])
    book.payment_reserved_by = user
    book.payment_reserved_until = locked.payment_reserved_until
    return True


def _clear_payment_hold(book, user=None):
    """Drop the payment hold (only the given user's, if one is named)."""
    if user is not None and book.payment_reserved_by_id != user.pk:
        return
    Book.objects.filter(pk=book.pk).update(payment_reserved_by=None, payment_reserved_until=None)
    book.payment_reserved_by = None
    book.payment_reserved_until = None


@login_required
@require_POST
@rate_limit('apply-coupon', 10, 60, json=True)
def apply_coupon(request, book_pk):
    """
    AJAX endpoint the checkout page's "Apply" button hits to preview a
    coupon's discount before the order is actually placed. Read-only - the
    real, order-locking redemption happens once in checkout() below via
    coupons.services.redeem_coupon(), so re-clicking "Apply" or refreshing
    this page never spends a use of the coupon.
    """
    book = get_object_or_404(Book, pk=book_pk)
    rent_days = _resolve_rent_days(book, request)
    if rent_days is not None:
        final_price = book.rent_price(rent_days)
    else:
        final_price, _unused = _resolve_final_price(book, request.user)
    code = request.POST.get('coupon_code', '')

    try:
        coupon, discount = validate_coupon(code, request.user, final_price)
    except CouponError as exc:
        return JsonResponse({'valid': False, 'error': str(exc)}, status=400)

    # No order exists yet to read delivery_charge from - mirror how
    # checkout() computes it: free for digital items, ₹40 for physical.
    delivery_charge = Decimal('0') if book.is_digital else Decimal('40')
    new_total = final_price + delivery_charge - discount
    return JsonResponse({
        'valid': True,
        'code': coupon.code,
        'discount': str(discount),
        'new_total': str(new_total),
        'message': coupon.description or f'Coupon applied - \u20b9{discount} off.',
    })


@login_required
def checkout(request, book_pk):
    book = get_object_or_404(Book, pk=book_pk)
    if book.seller == request.user:
        messages.error(request, _("You can't buy your own listing."))
        return redirect('books:detail', pk=book.pk)
    if book.status not in ('available', 'reserved'):
        messages.error(request, _('This book is no longer available.'))
        return redirect('books:detail', pk=book.pk)
    if _reserved_for_other_buyer(book, request.user):
        messages.error(request, _("This book is reserved for another buyer's accepted offer."))
        return redirect('books:detail', pk=book.pk)
    if book.is_payment_held_for_other(request.user):
        messages.error(request, _('Another buyer is completing payment for this book right now. '
                                'Please check back in a few minutes - it becomes available again if they do not finish.'))
        return redirect('books:detail', pk=book.pk)

    rent_days = _resolve_rent_days(book, request)
    is_rental = rent_days is not None
    if is_rental:
        # Rentals aren't subject to the offer/negotiation flow - they're
        # always priced off the current listed price, not an accepted offer.
        final_price, accepted_offer = book.rent_price(rent_days), None
    else:
        final_price, accepted_offer = _resolve_final_price(book, request.user)
    delivery_charge = Decimal('0') if book.is_digital else Decimal('40')

    if is_rental and book.status != 'available':
        messages.error(request, _('This book is no longer available to rent.'))
        return redirect('books:detail', pk=book.pk)

    if request.method == 'POST':
        form = CheckoutForm(request.POST, is_digital=book.is_digital)
        if form.is_valid():
            # Re-check availability at submit time too, not just on the GET
            # that rendered the form - closes the reservation race.
            book.refresh_from_db()
            if is_rental:
                if book.status != 'available':
                    messages.error(request, _('This book is no longer available to rent.'))
                    return redirect('books:detail', pk=book.pk)
            elif book.status not in ('available', 'reserved') or _reserved_for_other_buyer(book, request.user):
                messages.error(request, _('This book is no longer available.'))
                return redirect('books:detail', pk=book.pk)
            if book.is_payment_held_for_other(request.user):
                messages.error(request, _('Another buyer is completing payment for this book right now. Please try again shortly.'))
                return redirect('books:detail', pk=book.pk)

            # Money-safety net: never build an order from a price below the minimum
            # (a ₹0/negative listing or offer that predates the validators, or slipped past them).
            if final_price is None or final_price < MIN_PRICE:
                logger.warning('Refusing checkout of listing %s: invalid price %r', book.pk, final_price)
                messages.error(request, _('This listing has an invalid price and cannot be bought right now.'))
                return redirect('books:detail', pk=book.pk)

            order = form.save(commit=False)
            order.buyer = request.user
            order.book = book
            order.final_price = final_price
            order.delivery_charge = delivery_charge
            if is_rental:
                order.order_type = 'rental'
                order.rental_days = rent_days
            order.save()

            coupon_code = form.cleaned_data.get('coupon_code', '').strip()
            if coupon_code:
                try:
                    coupon, discount = validate_coupon(coupon_code, request.user, final_price)
                except CouponError as exc:
                    order.delete()
                    messages.error(request, str(exc))
                    return redirect('orders:checkout', book_pk=book.pk)
                order.coupon = coupon
                order.coupon_discount = discount
                if order.total_amount <= 0:  # a coupon may never make an order free or negative
                    order.delete()
                    messages.error(request, _('That coupon cannot be used on this order.'))
                    return redirect('orders:checkout', book_pk=book.pk)
                order.save(update_fields=['coupon', 'coupon_discount'])
                try:
                    redeem_coupon(coupon, request.user, order, discount)
                except CouponError as exc:  # the last use was taken between validation and now
                    order.delete()
                    messages.error(request, str(exc))
                    return redirect('orders:checkout', book_pk=book.pk)

            if order.payment_method == 'cod':
                if book.is_digital:
                    # Shouldn't happen - CheckoutForm strips 'cod' from the
                    # choices for digital books - but guard server-side too.
                    order.delete()
                    messages.error(request, _('Cash on Delivery is not available for digital items.'))
                    return redirect('orders:checkout', book_pk=book.pk)
                # Claim the listing before recording anything else, so a
                # buyer who loses the race walks away with no order and no
                # spent coupon rather than a confirmation for a book that's
                # already gone.
                if not _mark_book_taken(book, order):
                    order.delete()
                    messages.error(request, _('Sorry - someone else just bought this book.'))
                    return redirect('books:detail', pk=book.pk)
                # No gateway involved - money changes hands physically on
                # delivery, so there's nothing to "hold in escrow" here.
                Payment.objects.create(
                    order=order, amount=order.total_amount, method='cod', status='pending',
                )
                _create_real_shipment(order)
                notify_order_placed(order)
                messages.success(request, _('Order placed! The seller has been notified to prepare pickup.'))
                return redirect('orders:detail', order_id=order.order_id)

            if order.payment_method == 'bb_wallet':
                # Instant, internal payment: debit the buyer's BookBridge
                # Wallet balance right now. Money is already "with the
                # platform" once debited, so it goes straight to
                # escrow_held (same bookkeeping state a captured Razorpay
                # payment would be in) rather than 'pending'.
                if not _mark_book_taken(book, order):
                    order.delete()
                    messages.error(request, _('Sorry - someone else just bought this book.'))
                    return redirect('books:detail', pk=book.pk)
                try:
                    wallet_debit(
                        request.user, order.total_amount, 'payment', order=order,
                        description=f'Paid for order #{str(order.order_id)[:8]} ("{book.title}") from wallet',
                        idempotency_key=f'order-payment:{order.order_id}',
                    )
                except InsufficientBalance:
                    # Put the listing back - we claimed it a moment ago and
                    # the payment didn't happen, so it mustn't stay off the
                    # market.
                    _release_book_claim(book)
                    order.delete()
                    messages.error(request, _("Your wallet balance isn't enough to cover this order. "
                                             "Top it up via cashback/referrals or choose another payment method."))
                    return redirect('orders:checkout', book_pk=book.pk)

                Payment.objects.create(
                    order=order, amount=order.total_amount, method='bb_wallet', status='escrow_held',
                )
                if book.is_digital:
                    _deliver_digital_order(order)
                    messages.success(request, _('Paid from your BookBridge Wallet - your file is ready!'))
                else:
                    _create_real_shipment(order)
                    notify_order_placed(order)
                    messages.success(request, _('Paid from your BookBridge Wallet - order placed!'))
                return redirect('orders:detail', order_id=order.order_id)

            # Online payment: create a Razorpay Order and send the buyer to
            # the payment page. The book is NOT marked sold yet - that only
            # happens once payment actually succeeds (see payment_callback),
            # so a failed/abandoned payment doesn't lock the listing.
            # Hold the listing for this buyer while they pay, so nobody else can start paying for it.
            if not _reserve_for_payment(book, request.user):
                order.delete()
                messages.error(request, _('Another buyer is completing payment for this book right now, or it is no '
                                        'longer available. Please try again shortly.'))
                return redirect('books:detail', pk=book.pk)
            try:
                rp_order = razorpay_gateway.create_order(
                    amount_rupees=order.total_amount,
                    receipt=f'order-{order.order_id}',
                    notes={'order_id': str(order.order_id), 'book': book.title[:200]},
                )
            except razorpay_gateway.RazorpayConfigError as exc:
                _clear_payment_hold(book, request.user)
                order.delete()
                messages.error(request, str(exc))
                return redirect('orders:checkout', book_pk=book.pk)
            except Exception:
                logger.exception('Razorpay order creation failed for order %s', order.order_id)
                _clear_payment_hold(book, request.user)
                order.delete()
                messages.error(request, _('Could not start payment right now. Please try again.'))
                return redirect('orders:checkout', book_pk=book.pk)

            Payment.objects.create(
                order=order, amount=order.total_amount, method=order.payment_method,
                status='created', razorpay_order_id=rp_order['id'],
            )
            return redirect('orders:pay', order_id=order.order_id)
    else:
        initial = {}
        if not book.is_digital and request.user.address:
            initial = {'delivery_address': request.user.address, 'delivery_city': request.user.city,
                       'delivery_pincode': request.user.pincode}
        form = CheckoutForm(initial=initial, is_digital=book.is_digital)

    return render(request, 'orders/checkout.html', {
        'form': form, 'book': book, 'final_price': final_price, 'accepted_offer': accepted_offer,
        'wallet_balance': get_wallet(request.user).balance, 'delivery_charge': delivery_charge,
        'is_rental': is_rental, 'rent_days': rent_days,
    })


@transaction.atomic
def _deliver_digital_order(order):
    """
    Digital-book equivalent of the physical flow's confirm_delivery(): there's
    no courier/OTP step, so payment releases and delivery-linked wallet
    rewards fire immediately, and the buyer gets the file by email + a
    download link on the order page (see download_digital_file below).

    Takes the same row lock as _release_on_delivery() for the same reason:
    the Razorpay client callback and the webhook can both land on this path
    for one order, and cashback/referral rewards must only fire once.
    """
    # select_for_update() also refreshes, so numeric fields (e.g.
    # platform_commission_percent, whose model default is a plain float) come
    # back as proper Decimals from the DB - needed before
    # notify_digital_delivery reads order.seller_payout, since this is called
    # right after order.save() on a still-in-memory instance.
    locked = Order.objects.select_for_update().get(pk=order.pk)
    if locked.status == 'delivered':
        return order

    locked.status = 'delivered'
    locked.delivered_at = timezone.now()
    locked.save(update_fields=['status', 'delivered_at'])

    payment = getattr(locked, 'payment', None)
    if payment and payment.status in ('escrow_held', 'pending', 'created'):
        payment.status = 'released'
        payment.save(update_fields=['status'])
        ensure_payout(locked)

    award_delivery_cashback(locked)
    reward_referral_if_eligible(locked)
    notify_digital_delivery(locked)

    order.refresh_from_db()
    return order


def _create_real_shipment(order):
    """
    Creates the Shiprocket order + assigns a real courier/AWB for a
    just-finalized marketplace Order. Swallows and logs failures instead of
    blowing up checkout - a shipment can be retried by the seller/admin even
    if Shiprocket is briefly unreachable, whereas losing the buyer's paid
    order over a shipping-API hiccup would be worse.
    """
    shipment = Shipment.objects.create(order=order, delivery_otp=generate_otp())
    try:
        sr_order_id, sr_shipment_id = shiprocket.create_shipment_order(order)
        shipment.shiprocket_order_id = sr_order_id
        shipment.shiprocket_shipment_id = sr_shipment_id
        shipment.save(update_fields=['shiprocket_order_id', 'shiprocket_shipment_id'])

        awb_code, courier_name = shiprocket.assign_awb(sr_shipment_id)
        if awb_code:
            shipment.tracking_id = awb_code
            shipment.courier_name = courier_name
            shipment.save(update_fields=['tracking_id', 'courier_name'])
            shiprocket.request_pickup(sr_shipment_id)
    except shiprocket.ShiprocketConfigError as exc:
        logger.warning('Shiprocket not configured, shipment left unassigned: %s', exc)
    except Exception:
        logger.exception('Shiprocket shipment creation failed for order %s', order.order_id)
    return shipment


@login_required
def pay(request, order_id):
    """Renders the Razorpay Checkout page for an order awaiting online payment."""
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user)
    payment = getattr(order, 'payment', None)
    if not payment or payment.status != 'created':
        return redirect('orders:detail', order_id=order.order_id)

    return render(request, 'orders/pay.html', {
        'order': order,
        'payment': payment,
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
        'amount_paise': int(round(float(order.total_amount) * 100)),
        'buyer_name': request.user.get_full_name() or request.user.username,
        'buyer_email': request.user.email,
        'buyer_phone': request.user.phone,
    })


@login_required
@require_POST
def payment_callback(request, order_id):
    """Razorpay Checkout's client-side success handler posts here."""
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user)
    payment = getattr(order, 'payment', None)
    if not payment or payment.status != 'created':
        messages.info(request, _('This order has already been processed.'))
        return redirect('orders:detail', order_id=order.order_id)

    rp_order_id = request.POST.get('razorpay_order_id', '')
    rp_payment_id = request.POST.get('razorpay_payment_id', '')
    rp_signature = request.POST.get('razorpay_signature', '')

    # 1) The Razorpay order in the callback must be the one WE created for
    #    THIS BookBridge order. A signature alone doesn't prove that: a buyer
    #    could replay a genuine, cheaper (order, payment, signature) triple
    #    from a different checkout. Not marked 'failed' - a forged callback
    #    shouldn't be able to cancel a genuine payment that is still pending.
    if not rp_order_id or not hmac.compare_digest(rp_order_id, payment.razorpay_order_id or ''):
        logger.warning('payment_callback order mismatch for %s (got %r)', order.order_id, rp_order_id)
        messages.error(request, _('Payment verification failed. Please try again.'))
        return redirect('orders:pay', order_id=order.order_id)

    # 2) Signature check.
    try:
        razorpay_gateway.verify_payment_signature(rp_order_id, rp_payment_id, rp_signature)
    except razorpay.errors.SignatureVerificationError:
        payment.status = 'failed'
        payment.save(update_fields=['status'])
        _clear_payment_hold(order.book, order.buyer)
        messages.error(request, _('Payment verification failed. If money was deducted, it will be auto-refunded '
                                 'by Razorpay within a few days; otherwise please try again.'))
        return redirect('orders:pay', order_id=order.order_id)

    # 3) Ask Razorpay directly: right order, right amount, right currency, captured.
    state, reason = razorpay_gateway.verify_payment_for_order(rp_payment_id, payment)
    if state == 'invalid':
        logger.warning('payment_callback API check failed for %s: %s', order.order_id, reason)
        messages.error(request, _('We could not confirm this payment with Razorpay. If money was deducted, it '
                                 'will be refunded automatically; otherwise please try again.'))
        return redirect('orders:pay', order_id=order.order_id)
    if state == 'pending':
        messages.info(request, _('Your payment is being processed. Your order will be confirmed in a moment.'))
        return redirect('orders:detail', order_id=order.order_id)

    _finalize_paid_order(order, payment, rp_payment_id, rp_signature)
    messages.success(request, _('Payment successful! Your order has been placed.'))
    return redirect('orders:detail', order_id=order.order_id)


@transaction.atomic
def _claim_payment_for_finalization(payment, rp_payment_id, rp_signature):
    """
    Moves a Payment from 'created' to 'escrow_held' under a row lock, and
    reports whether *this* caller was the one that made the move.

    The buyer's client-side Razorpay callback and Razorpay's server-to-server
    webhook routinely arrive within milliseconds of each other for the same
    order. An unlocked `if payment.status == 'escrow_held': return` check lets
    both pass, which means two shipments and two notification fan-outs for one
    payment. Locking the row makes the loser wait, re-read, and back out.
    """
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status == 'escrow_held':
        return False
    locked.status = 'escrow_held'
    locked.razorpay_payment_id = rp_payment_id
    locked.razorpay_signature = rp_signature
    locked.transaction_ref = rp_payment_id
    locked.save(update_fields=['status', 'razorpay_payment_id', 'razorpay_signature', 'transaction_ref'])

    # Keep the caller's instance in step - order_detail and the notify
    # helpers read off it after this returns.
    payment.status = locked.status
    payment.razorpay_payment_id = locked.razorpay_payment_id
    payment.razorpay_signature = locked.razorpay_signature
    payment.transaction_ref = locked.transaction_ref
    return True


def _finalize_paid_order(order, payment, rp_payment_id, rp_signature):
    """Shared by the client callback and the webhook so either path (whichever fires first) is safe to call twice."""
    if not _claim_payment_for_finalization(payment, rp_payment_id, rp_signature):
        return  # another request already finalized this payment

    book = order.book
    taken_status = 'rented' if order.order_type == 'rental' else 'sold'
    if book.status != taken_status and not _mark_book_taken(book, order):
        # The buyer's money is captured but the listing went to someone
        # else while they were on the Razorpay page. Nothing to do here
        # automatically - surface it loudly so it can be refunded from the
        # admin's return/refund flow.
        logger.error(
            'Order %s was paid for but listing %s was already taken - needs a manual refund.',
            order.order_id, book.pk,
        )

    if book.is_digital:
        if order.status != 'delivered':
            _deliver_digital_order(order)
        return

    if not hasattr(order, 'shipment'):
        _create_real_shipment(order)
    notify_order_placed(order)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """
    Server-to-server backstop for payment confirmation: fires even if the
    buyer closes the tab before the client-side redirect completes. Verify
    the signature before trusting anything in the body.
    """
    signature = request.headers.get('X-Razorpay-Signature', '')
    if not razorpay_gateway.verify_webhook_signature(request.body, signature):
        return HttpResponseBadRequest('invalid signature')

    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest('invalid json')

    event_type = event.get('event')
    payload = event.get('payload', {}).get('payment', {}).get('entity', {})
    rp_order_id = payload.get('order_id', '')
    rp_payment_id = payload.get('id', '')

    if not rp_order_id:
        return HttpResponse(status=200)

    try:
        payment = Payment.objects.get(razorpay_order_id=rp_order_id)
    except Payment.DoesNotExist:
        return HttpResponse(status=200)

    if event_type == 'payment.captured':
        ok, reason = razorpay_gateway.check_payment_against_payment_row(payload, payment)
        if not ok:
            logger.error('Razorpay webhook payment.captured rejected for %s: %s', payment.order.order_id, reason)
            return HttpResponse(status=200)
        _finalize_paid_order(payment.order, payment, rp_payment_id, payload.get('signature', 'webhook'))
    elif event_type == 'payment.failed' and payment.status == 'created':
        payment.status = 'failed'
        payment.save(update_fields=['status'])
        _clear_payment_hold(payment.order.book, payment.order.buyer)
    elif event_type == 'refund.processed':
        payment.status = 'refunded'
        payment.save(update_fields=['status'])

    return HttpResponse(status=200)


@csrf_exempt
@require_POST
def shiprocket_webhook(request):
    """Push updates from Shiprocket as a courier scans a shipment through its journey."""
    token = request.headers.get('X-Api-Key', '')
    if not settings.SHIPROCKET_WEBHOOK_TOKEN or not hmac.compare_digest(
        token, settings.SHIPROCKET_WEBHOOK_TOKEN
    ):
        return HttpResponseBadRequest('invalid token')

    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest('invalid json')

    awb = event.get('awb') or event.get('awb_code')
    if not awb:
        return HttpResponse(status=200)

    try:
        shipment = Shipment.objects.get(tracking_id=awb)
    except Shipment.DoesNotExist:
        return HttpResponse(status=200)

    _apply_tracking_update(shipment, current_status_raw=event.get('current_status'))
    return HttpResponse(status=200)


def _apply_tracking_update(shipment, current_status_raw=None):
    """Pulls fresh tracking activity from Shiprocket and syncs local status (forward-only)."""
    try:
        normalized, events = shiprocket.track_by_awb(shipment.tracking_id)
    except Exception:
        logger.exception('Shiprocket tracking fetch failed for AWB %s', shipment.tracking_id)
        return shipment

    shipment.tracking_history = json.dumps(events)
    shipment.tracking_synced_at = timezone.now()

    status = normalized or shiprocket.normalize_status(current_status_raw)
    order_reached = {
        'pickup_pending': 0, 'picked_up': 1, 'in_transit': 2,
        'out_for_delivery': 3, 'delivered': 4, 'failed': -1, 'cancelled': -1,
    }
    if status and order_reached.get(status, -1) > order_reached.get(shipment.status, -1):
        shipment.status = status
        order = shipment.order
        # Mirror shipment progress onto the order for statuses short of
        # "delivered" - delivery/payment-release still requires the
        # buyer's OTP confirmation regardless of what the courier reports,
        # since that's the marketplace's own trust checkpoint, not the
        # courier's.
        status_to_order = {'picked_up': 'accepted', 'in_transit': 'shipped', 'out_for_delivery': 'out_for_delivery'}
        if status in status_to_order and order.status != 'delivered':
            order.status = status_to_order[status]
            order.save(update_fields=['status'])
            notify_order_status_update(order)
            if status == 'out_for_delivery':
                notify_delivery_otp(order)

    shipment.save(update_fields=['tracking_history', 'tracking_synced_at', 'status'])
    return shipment


@login_required
@require_POST
def refresh_tracking(request, order_id):
    order = get_object_or_404(Order, order_id=order_id)
    if request.user not in (order.buyer, order.book.seller):
        messages.error(request, _('You do not have access to this order.'))
        return redirect('books:home')

    shipment = getattr(order, 'shipment', None)
    if not shipment or not shipment.tracking_id:
        messages.info(request, _('No courier assigned yet.'))
    else:
        _apply_tracking_update(shipment)
        messages.success(request, _('Tracking updated.'))
    return redirect('orders:detail', order_id=order.order_id)


@login_required
def download_digital_file(request, order_id):
    """
    Gated download for a digital order - checked against the buyer/order
    instead of just serving books/digital_file's media URL directly, so a
    guessed/shared link can't pull the file without having actually paid.
    """
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user)
    book = order.book
    if not book.is_digital or not book.digital_file:
        messages.error(request, _('No digital file is attached to this order.'))
        return redirect('orders:detail', order_id=order.order_id)
    if order.status != 'delivered':
        messages.error(request, _('Your payment is still being processed - the download will unlock once it clears.'))
        return redirect('orders:detail', order_id=order.order_id)

    from django.http import FileResponse
    try:
        handle = book.digital_file.open('rb')
    except (FileNotFoundError, ValueError):
        messages.error(request, _('The file is temporarily unavailable. Please contact support.'))
        return redirect('orders:detail', order_id=order.order_id)
    response = FileResponse(handle, as_attachment=True, filename=book.digital_file.name.split('/')[-1])
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(Order, order_id=order_id)
    if request.user not in (order.buyer, order.book.seller):
        messages.error(request, _('You do not have access to this order.'))
        return redirect('books:home')

    review = getattr(order, 'review', None)
    return_request = getattr(order, 'return_request', None)
    shipment = getattr(order, 'shipment', None)
    dispute = order.disputes.exclude(status='withdrawn').first()
    can_dispute = request.user == order.buyer and not dispute and not dispute_blocked_reason(order)
    return render(request, 'orders/order_detail.html', {
        'order': order, 'is_seller': request.user == order.book.seller, 'review': review,
        'return_request': return_request, 'dispute': dispute, 'can_dispute': can_dispute,
        'tracking_events': shipment.tracking_events() if shipment else [],
        'twilio_configured': bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN),
    })


@login_required
def download_invoice(request, order_id):
    """Buyer/seller-facing bill for a placed order. Only available once
    there's a confirmed payment record - for online payments that means
    past Razorpay's 'created' (order opened, not yet paid) state, so a
    buyer can't download a receipt for money that was never actually
    collected."""
    order = get_object_or_404(Order, order_id=order_id)
    if request.user not in (order.buyer, order.book.seller):
        messages.error(request, _('You do not have access to this order.'))
        return redirect('books:home')

    payment = getattr(order, 'payment', None)
    if not payment or payment.status in ('created', 'failed'):
        messages.error(request, _('No confirmed payment yet for this order - invoice isn\'t available.'))
        return redirect('orders:detail', order_id=order.order_id)

    pdf_bytes = render_invoice_pdf(order)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="invoice-{str(order.order_id)[:8]}.pdf"'
    return response


@login_required
def my_orders(request):
    purchases = Order.objects.filter(buyer=request.user)
    sales = Order.objects.filter(book__seller=request.user)
    return render(request, 'orders/my_orders.html', {'purchases': purchases, 'sales': sales})


@login_required
@require_POST
def update_order_status(request, order_id):
    """Seller-side status progression: accepted -> shipped -> out_for_delivery."""
    order = get_object_or_404(Order, order_id=order_id, book__seller=request.user)
    new_status = request.POST.get('status')
    valid_transitions = {
        'placed': 'accepted', 'accepted': 'shipped', 'shipped': 'out_for_delivery',
    }
    if new_status == valid_transitions.get(order.status):
        order.status = new_status
        order.save(update_fields=['status'])
        shipment_status_map = {
            'accepted': 'pickup_pending', 'shipped': 'in_transit', 'out_for_delivery': 'out_for_delivery',
        }
        if hasattr(order, 'shipment'):
            order.shipment.status = shipment_status_map.get(new_status, order.shipment.status)
            order.shipment.save(update_fields=['status'])
        messages.success(request, _('Order marked as %(status)s.') % {'status': order.get_status_display()})
        notify_order_status_update(order)
        if new_status == 'out_for_delivery':
            notify_delivery_otp(order)
    return redirect('orders:detail', order_id=order.order_id)


@login_required
@require_POST
def confirm_delivery(request, order_id):
    """Buyer confirms delivery with OTP -> releases escrow payment."""
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user)

    # Guard against replay: the OTP stays valid after a successful
    # confirmation, so without this an already-delivered order could be
    # re-confirmed by POSTing the same code again - crediting cashback
    # every time. Escrow release and wallet rewards must happen exactly
    # once per order.
    if order.status == 'delivered':
        messages.info(request, _('This delivery has already been confirmed.'))
        return redirect('orders:detail', order_id=order.order_id)

    otp = request.POST.get('otp', '')
    shipment = getattr(order, 'shipment', None)
    if shipment and shipment.delivery_otp_attempts >= MAX_DELIVERY_OTP_ATTEMPTS:
        messages.error(
            request,
            _('Too many incorrect attempts. Please contact support to verify delivery.'),
        )
        return redirect('orders:detail', order_id=order.order_id)
    if not (shipment and shipment.check_delivery_otp(otp)):
        messages.error(request, _('Incorrect OTP. Please check with your delivery partner.'))
        return redirect('orders:detail', order_id=order.order_id)

    confirmed = _release_on_delivery(order, shipment)
    if not confirmed:
        # Another request (a double-submitted form, a refresh) won the race
        # and already ran the release. Nothing more to do here.
        messages.info(request, _('This delivery has already been confirmed.'))
        return redirect('orders:detail', order_id=order.order_id)

    messages.success(request, _('Delivery confirmed! Payment released to the seller.'))
    notify_delivery_confirmed(order)
    return redirect('orders:detail', order_id=order.order_id)


@transaction.atomic
def _release_on_delivery(order, shipment):
    """
    Marks `order` delivered, releases escrow, and pays out the delivery-linked
    wallet rewards - as one all-or-nothing unit, exactly once.

    Returns False if the order was already delivered by the time we took the
    row lock, so the caller knows not to re-notify. The lock is what makes
    two simultaneous POSTs of the same OTP safe: the second one blocks here,
    then re-reads status='delivered' and bails out.
    """
    locked = Order.objects.select_for_update().get(pk=order.pk)
    if locked.status == 'delivered':
        return False

    locked.status = 'delivered'
    locked.delivered_at = timezone.now()
    update_fields = ['status', 'delivered_at']
    if locked.is_rental and locked.rental_days:
        # The rental clock starts now, once the buyer actually has
        # the book in hand - not back at checkout time.
        locked.rental_due_date = timezone.now() + timedelta(days=locked.rental_days)
        update_fields.append('rental_due_date')
    locked.save(update_fields=update_fields)

    if shipment:
        shipment.status = 'delivered'
        shipment.save(update_fields=['status'])

    payment = getattr(locked, 'payment', None)
    if payment and payment.status in ('escrow_held', 'pending'):
        # escrow_held -> released for real gateway payments;
        # pending -> released for COD, meaning "payout now due" -
        # there was never a gateway hold to release in that case.
        payment.status = 'released'
        payment.save(update_fields=['status'])
        ensure_payout(locked)

    # Wallet rewards, tied to a real completed delivery (not just an
    # order being placed) so they can't be farmed by abandoning or
    # cancelling orders.
    award_delivery_cashback(locked)
    reward_referral_if_eligible(locked)

    # Keep the caller's in-memory instance in step with what we just wrote,
    # since notify_delivery_confirmed() reads off it.
    order.status = locked.status
    order.rental_due_date = locked.rental_due_date
    return True


@login_required
@require_POST
def mark_book_returned(request, order_id):
    """
    Buyer self-reports that they've sent the rented book back to the
    seller. Trust-based, same MVP spirit as the delivery OTP being shown
    on-screen when SMS isn't configured - there's no reverse-shipment
    tracking here, just freeing the listing back up once the rental is done.
    """
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user, order_type='rental')
    if order.status == 'delivered':
        order.status = 'returned'
        order.save(update_fields=['status'])
        order.book.status = 'available'
        order.book.save(update_fields=['status'])
        messages.success(request, _('Marked as returned. Thanks for renting responsibly!'))
        notify_order_status_update(order)
    return redirect('orders:detail', order_id=order.order_id)


@login_required
def leave_review(request, order_id):
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user, status='delivered')
    if hasattr(order, 'review'):
        messages.info(request, _('You already reviewed this order.'))
        return redirect('orders:detail', order_id=order.order_id)
    if request.method == 'POST':
        form = ReviewForm(request.POST)
        if form.is_valid():
            review = form.save(commit=False)
            review.order = order
            review.save()
            messages.success(request, _('Thanks for your review!'))
            return redirect('orders:detail', order_id=order.order_id)
    else:
        form = ReviewForm()
    return render(request, 'orders/leave_review.html', {'form': form, 'order': order})


@login_required
def request_return(request, order_id):
    order = get_object_or_404(Order, order_id=order_id, buyer=request.user, status='delivered')
    if hasattr(order, 'return_request'):
        messages.info(request, _('A return request already exists for this order.'))
        return redirect('orders:detail', order_id=order.order_id)
    if request.method == 'POST':
        form = ReturnRequestForm(request.POST)
        if form.is_valid():
            rr = form.save(commit=False)
            rr.order = order
            rr.save()
            messages.success(request, _('Return request submitted. Our team will review it shortly.'))
            notify_return_submitted(rr)
            return redirect('orders:detail', order_id=order.order_id)
    else:
        form = ReturnRequestForm()
    return render(request, 'orders/request_return.html', {'form': form, 'order': order})
