"""
Real transactional notifications: email via Django's SMTP backend, SMS +
WhatsApp via Twilio, Web Push via VAPID (pywebpush), and an in-app
Notification Center (the `notifications` app).

Nothing here is faked - if EMAIL_HOST, TWILIO_*, or WEBPUSH_VAPID_* aren't
configured, calls degrade gracefully (logged, return False) instead of
raising, matching the same pattern as orders/payments.py and
orders/shiprocket.py. In local dev with no EMAIL_HOST set, Django's console
backend just prints the email to your runserver terminal, so you can see
exactly what would have been sent without setting anything up.

Two layers:
  - send_email / send_sms / send_whatsapp / send_push - one channel each,
    used directly for input-a-code-right-now OTPs where a preference check
    doesn't make sense (see notify_email_otp below).
  - dispatch() - the fan-out used by every other event function below: one
    call creates the in-app Notification and sends across whichever of
    email/push/SMS/WhatsApp channels the recipient has opted into, per
    notifications.models.NotificationPreference.
"""
import json
import logging

from django.conf import settings
from django.core.mail import send_mail, EmailMessage

logger = logging.getLogger(__name__)


# --- Email -------------------------------------------------------------

def send_email(to_email, subject, body):
    if not to_email:
        return False
    try:
        send_mail(
            subject=f'[{settings.SITE_NAME}] {subject}',
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception('Failed to send email to %s: %s', to_email, subject)
        return False


def send_email_with_attachment(to_email, subject, body, *, attachment_bytes, attachment_filename, mimetype):
    """Same delivery semantics as send_email (degrades to a logged failure
    rather than raising - see module docstring) but for the one case that
    needs a file on it: the order invoice. Uses EmailMessage directly since
    send_mail()/Django's console backend don't support attachments."""
    if not to_email:
        return False
    try:
        message = EmailMessage(
            subject=f'[{settings.SITE_NAME}] {subject}',
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
        )
        message.attach(attachment_filename, attachment_bytes, mimetype)
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception('Failed to send email with attachment to %s: %s', to_email, subject)
        return False


# --- SMS / WhatsApp (Twilio) -------------------------------------------

class TwilioConfigError(Exception):
    """Raised when TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN aren't configured."""


def _twilio_client():
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
        raise TwilioConfigError(
            'TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set. '
            'Add them to your environment (see .env.example) to send real SMS/WhatsApp messages.'
        )
    from twilio.rest import Client
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def _to_e164(phone: str) -> str:
    """Normalizes a stored phone number to E.164 for Twilio. Assumes India
    (+91) for 10-digit local numbers, since that's this app's target market -
    adjust if you're deploying elsewhere."""
    phone = (phone or '').strip().replace(' ', '').replace('-', '')
    if not phone:
        return ''
    if phone.startswith('+'):
        return phone
    if len(phone) == 10 and phone.isdigit():
        return f'+91{phone}'
    return f'+{phone}' if phone.isdigit() else phone


def send_sms(to_phone, body):
    e164 = _to_e164(to_phone)
    if not e164:
        return False
    try:
        client = _twilio_client()
        client.messages.create(body=body, from_=settings.TWILIO_SMS_FROM, to=e164)
        return True
    except TwilioConfigError as exc:
        logger.warning(str(exc))
        return False
    except Exception:
        logger.exception('Failed to send SMS to %s', to_phone)
        return False


def send_whatsapp(to_phone, body):
    e164 = _to_e164(to_phone)
    if not e164:
        return False
    try:
        client = _twilio_client()
        client.messages.create(
            body=body,
            from_=settings.TWILIO_WHATSAPP_FROM,
            to=f'whatsapp:{e164}',
        )
        return True
    except TwilioConfigError as exc:
        logger.warning(str(exc))
        return False
    except Exception:
        logger.exception('Failed to send WhatsApp message to %s', to_phone)
        return False


def notify_phone(to_phone, body):
    """
    Tries WhatsApp first (richer, usually preferred in India for this kind
    of update), falls back to plain SMS if WhatsApp isn't configured/fails
    - e.g. the recipient never messaged your Twilio WhatsApp sandbox number,
    which Twilio's sandbox requires before it'll deliver to them. Used
    directly for OTPs (see below), which always go out regardless of the
    recipient's SMS/WhatsApp notification preference - dispatch() below
    handles the opt-in/out version for everything else.
    """
    if settings.NOTIFY_WHATSAPP_FIRST and send_whatsapp(to_phone, body):
        return True
    return send_sms(to_phone, body)


# --- Push (Web Push / VAPID) --------------------------------------------

def send_push(user, title, body, url=''):
    """
    Web Push to every browser/device `user` has subscribed on (see
    notifications.PushSubscription, populated by static/js/push.js via
    notifications:push_subscribe). Degrades gracefully - logged, returns
    False - if WEBPUSH_VAPID_PRIVATE_KEY isn't set or pywebpush isn't
    installed, same pattern as Twilio above. A subscription the push
    service reports as gone (404/410 - browser uninstalled, permission
    revoked, etc.) is deleted so we stop wasting a request on it every time.
    """
    if not (settings.WEBPUSH_VAPID_PRIVATE_KEY and settings.WEBPUSH_VAPID_PUBLIC_KEY):
        logger.info('WEBPUSH_VAPID_* not configured - skipping push to %s.', user.username)
        return False
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning('pywebpush is not installed - skipping push to %s. `pip install pywebpush`.', user.username)
        return False

    from notifications.models import PushSubscription

    payload = json.dumps({'title': f'{settings.SITE_NAME}: {title}', 'body': body, 'url': url or '/'})
    sent = False
    for sub in PushSubscription.objects.filter(user=user):
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
                vapid_claims={'sub': f'mailto:{settings.WEBPUSH_VAPID_CLAIMS_EMAIL}'},
            )
            sent = True
        except WebPushException as exc:
            status = getattr(exc.response, 'status_code', None)
            if status in (404, 410):
                sub.delete()
            else:
                logger.warning('Push to %s failed (status %s): %s', user.username, status, exc)
        except Exception:
            logger.exception('Push to %s failed unexpectedly.', user.username)
    return sent


# --- Multi-channel dispatch + in-app Notification Center ----------------

def dispatch(user, *, title, body, url='', email_subject=None, email_body=None, sms_body=None, allow_phone=True,
             send_email_channel=True):
    """
    The fan-out every event function below calls once instead of hand-
    rolling per-channel logic: creates the in-app Notification (Notification
    Center, always the source of truth for "what happened"), then sends
    across whichever of email/push/SMS/WhatsApp `user` has actually opted
    into - notifications.models.NotificationPreference.

    `body` is the short in-app/push/SMS text. Pass email_subject/email_body
    when the email should carry more detail than that (most events already
    had a nicely-written multi-line email body before dispatch() existed -
    those keep using it unchanged). `allow_phone=False` skips SMS/WhatsApp
    entirely for events that aren't worth a text even if the user opted in
    generally (e.g. a running chat conversation). `send_email_channel=False` skips the
    plain email when the caller sends its own (e.g. one with an attachment).
    """
    from notifications.models import Notification, NotificationPreference

    if user is None or not getattr(user, 'pk', None):
        return

    prefs = NotificationPreference.for_user(user)

    if prefs.in_app_enabled:
        Notification.objects.create(user=user, title=title, body=body, url=url)

    if prefs.email_enabled and send_email_channel:
        send_email(user.email, email_subject or title, email_body or body)

    if prefs.push_enabled:
        send_push(user, title, body, url=url)

    if allow_phone and user.phone:
        phone_body = sms_body or body
        if prefs.whatsapp_enabled and send_whatsapp(user.phone, phone_body):
            pass
        elif prefs.sms_enabled:
            send_sms(user.phone, phone_body)


# --- Event-specific notifications ---------------------------------------
# Each of these is called from the view at the point the underlying state
# actually changes - not from a signal - so it's obvious from reading the
# view what triggers a notification, matching how this app already calls
# out to Razorpay/Shiprocket inline rather than via signals.

def notify_signup_welcome(user):
    send_email(
        user.email,
        'Welcome to BookBridge!',
        f'Hi {user.get_full_name() or user.username},\n\n'
        f'Your BookBridge account is ready. Start browsing used books or list '
        f'one of your own from your dashboard.\n\n- The BookBridge Team',
    )


def notify_new_offer(offer):
    dispatch(
        offer.book.seller,
        title=f'New offer on "{offer.book.title}"',
        body=f'{offer.buyer.username} offered \u20b9{offer.amount} (asking \u20b9{offer.book.price}).',
        url=f'/book/{offer.book.pk}/',
        email_subject=f'New offer on "{offer.book.title}"',
        email_body=(
            f'{offer.buyer.username} offered \u20b9{offer.amount} for your listing '
            f'"{offer.book.title}" (asking price \u20b9{offer.book.price}).\n\n'
            f'Log in to accept, reject, or counter: {settings.SITE_URL}/book/{offer.book.pk}/'
        ),
    )


def notify_offer_response(offer):
    """offer.status is one of accepted/rejected/countered; buyer-facing."""
    if offer.status == 'accepted':
        title = f'Your offer on "{offer.book.title}" was accepted!'
        body = f'Your offer of \u20b9{offer.amount} was accepted - check out before someone else does.'
        email_body = (
            f'Good news - your offer of \u20b9{offer.amount} for "{offer.book.title}" was accepted. '
            f'Check out now before someone else does: {settings.SITE_URL}/orders/checkout/{offer.book.pk}/'
        )
        url = f'/orders/checkout/{offer.book.pk}/'
    elif offer.status == 'rejected':
        title = f'Your offer on "{offer.book.title}" was declined'
        body = email_body = f'Your offer of \u20b9{offer.amount} for "{offer.book.title}" was declined by the seller.'
        url = f'/book/{offer.book.pk}/'
    else:
        return  # 'countered' - the new counter-Offer triggers its own notification via notify_new_offer-style flow
    dispatch(offer.buyer, title=title, body=body, url=url, email_body=email_body)


def notify_order_placed(order):
    buyer, seller, book = order.buyer, order.book.seller, order.book
    kind = f'rental ({order.rental_days} days)' if order.is_rental else 'order'
    order_url = f'/orders/order/{order.order_id}/'
    dispatch(
        buyer,
        title=f'Order confirmed - {book.title}',
        body=f'Your {kind} for "{book.title}" is confirmed. Total paid: \u20b9{order.total_amount}.',
        url=order_url,
        email_body=(
            f'Your {kind} for "{book.title}" is confirmed.\nOrder ID: {str(order.order_id)[:8]}\n'
            f'Total paid: \u20b9{order.total_amount}\nTrack it here: {settings.SITE_URL}{order_url}'
        ),
    )
    dispatch(
        seller,
        title=f'New {"rental" if order.is_rental else "order"} - {book.title}',
        body=f'{buyer.username} just {"rented" if order.is_rental else "ordered"} "{book.title}" for \u20b9{order.final_price}.',
        url=order_url,
        email_body=(
            f'{buyer.username} just {"rented" if order.is_rental else "ordered"} "{book.title}" for \u20b9{order.final_price}'
            f'{f" ({order.rental_days} days)" if order.is_rental else ""}. '
            f'Please prepare it for pickup.\nOrder ID: {str(order.order_id)[:8]}\n'
            f'View order: {settings.SITE_URL}{order_url}'
        ),
    )
    _email_invoice(order)


def _email_invoice(order):
    """Auto-emails the PDF invoice/receipt (orders/invoice.py) to both
    buyer and seller as soon as an order is confirmed, on top of the
    in-app/push/SMS notification above - so neither side has to remember
    to click 'Download Invoice' on the order page. Called only from
    notify_order_placed, which by construction only fires once a Payment
    already exists in a non-created/non-failed state (see
    orders.views._finalize_paid_order and the two checkout branches), so
    render_invoice_pdf always has real payment details to show. Best-
    effort like every other channel here: a failed send is logged, not
    raised, so it can never block order placement."""
    from notifications.models import NotificationPreference
    from orders.invoice import render_invoice_pdf

    try:
        pdf_bytes = render_invoice_pdf(order)
    except Exception:
        logger.exception('Failed to render invoice PDF for order %s', order.order_id)
        return

    filename = f'invoice-{str(order.order_id)[:8]}.pdf'
    subject = f'Invoice for your order - {order.book.title}'
    body = (
        f'Thanks for your order on {settings.SITE_NAME}!\n\n'
        f'Please find attached the invoice/receipt for order #{str(order.order_id)[:8]} '
        f'("{order.book.title}", \u20b9{order.total_amount}).'
    )
    for recipient in (order.buyer, order.book.seller):
        if NotificationPreference.for_user(recipient).email_enabled:
            send_email_with_attachment(
                recipient.email, subject, body,
                attachment_bytes=pdf_bytes, attachment_filename=filename, mimetype='application/pdf',
            )


def notify_delivery_otp(order):
    """
    Sent when the shipment reaches 'out_for_delivery' - the realistic moment
    for a delivery OTP, not at order placement (a courier scan weeks later
    is when the OTP is actually needed). Goes straight out via notify_phone,
    not dispatch() - a delivery OTP isn't optional/preference-gated, the
    buyer needs it to receive their book regardless of their SMS/WhatsApp
    notification settings.
    """
    shipment = getattr(order, 'shipment', None)
    if not shipment or not shipment.delivery_otp:
        return False
    body = (f'Your BookBridge delivery OTP for "{order.book.title}" is {shipment.delivery_otp}. '
            f'Share it with the delivery agent only after you receive your book. Do not share it before that.')
    return notify_phone(order.buyer.phone, body)


def notify_order_status_update(order):
    status_messages = {
        'accepted': 'has been accepted by the seller and will be picked up soon.',
        'shipped': 'has been shipped.',
        'out_for_delivery': 'is out for delivery today.',
    }
    if order.status not in status_messages:
        return
    order_url = f'/orders/order/{order.order_id}/'
    dispatch(
        order.buyer,
        title=f'Order update - {order.book.title}',
        body=f'Your order for "{order.book.title}" {status_messages[order.status]}',
        url=order_url,
        email_body=(
            f'Your order for "{order.book.title}" {status_messages[order.status]}\n'
            f'Track it: {settings.SITE_URL}{order_url}'
        ),
    )


def notify_delivery_confirmed(order):
    order_url = f'/orders/order/{order.order_id}/'
    dispatch(
        order.buyer,
        title=f'Delivered - {order.book.title}',
        body=f'Delivery of "{order.book.title}" is confirmed. Thanks for buying on BookBridge!',
        url=order_url,
        email_body=(
            f'Delivery of "{order.book.title}" is confirmed. Thanks for buying on BookBridge! '
            f'You can leave a review or request a return from your order page.'
        ),
    )
    dispatch(
        order.book.seller,
        title=f'Delivery confirmed - payout due for {order.book.title}',
        body=f'Payout of \u20b9{order.seller_payout} is now due for "{order.book.title}".',
        url=order_url,
        email_body=(
            f'The buyer confirmed delivery of "{order.book.title}". Your payout of \u20b9{order.seller_payout} '
            f'(after {order.platform_commission_percent}% commission) is now due.'
        ),
    )


def notify_digital_delivery(order):
    """
    Sent the moment a digital order (PDF Notes / Handwritten Notes /
    Question Papers / Lab Manuals) is paid for - there's no courier, so this
    plays the role notify_order_placed + notify_delivery_confirmed play
    together for physical orders.
    """
    buyer, seller, book = order.buyer, order.book.seller, order.book
    order_url = f'/orders/order/{order.order_id}/'
    download_url = f'{order_url}download/'
    dispatch(
        buyer,
        title=f'Your download is ready - {book.title}',
        body=f'"{book.title}" ({book.get_digital_type_display()}) is ready to download.',
        url=download_url,
        email_body=(
            f'Your purchase of "{book.title}" ({book.get_digital_type_display()}) is confirmed.\n'
            f'Order ID: {str(order.order_id)[:8]}\nAmount paid: \u20b9{order.total_amount}\n\n'
            f'Download it here: {settings.SITE_URL}{download_url}\n'
            f'(You can also grab it anytime from your order page: {settings.SITE_URL}{order_url})'
        ),
    )
    dispatch(
        seller,
        title=f'Your digital listing sold - {book.title}',
        body=f'{buyer.username} bought "{book.title}" for \u20b9{order.final_price}. Payout: \u20b9{order.seller_payout}.',
        url=order_url,
        email_body=(
            f'{buyer.username} just bought "{book.title}" for \u20b9{order.final_price}. '
            f'Your payout of \u20b9{order.seller_payout} (after {order.platform_commission_percent}% commission) is now due.'
        ),
    )


def notify_return_submitted(return_request):
    order = return_request.order
    dispatch(
        order.book.seller,
        title=f'Return requested - {order.book.title}',
        body=f'{order.buyer.username} requested a return - reason: {return_request.get_reason_display()}.',
        url=f'/orders/order/{order.order_id}/',
        email_body=(
            f'{order.buyer.username} requested a return on "{order.book.title}" - '
            f'reason: {return_request.get_reason_display()}.\n{return_request.details}\n\n'
            f'This will be reviewed by BookBridge support.'
        ),
    )


def notify_email_otp(user, otp):
    return send_email(
        user.email,
        'Your email verification code',
        f'Hi {user.get_full_name() or user.username},\n\n'
        f'Your BookBridge email verification code is {otp}. It expires in 10 minutes.\n'
        f'If you did not request this, you can ignore this email.\n\n- The BookBridge Team',
    )


def notify_saved_search_match(saved_search, book):
    """
    Sent when a newly-listed book matches a buyer's SavedSearch - fired from
    books/views.py:add_book right after the listing is published, using
    SavedSearch.matches() to decide who qualifies.
    """
    dispatch(
        saved_search.user,
        title=f'New match for "{saved_search.label}"',
        body=f'"{book.title}" - \u20b9{book.price} ({book.get_condition_display()}) matches your saved search.',
        url=f'/book/{book.pk}/',
        email_body=(
            f'A book just listed matches your saved search "{saved_search.label}":\n\n'
            f'"{book.title}" - \u20b9{book.price} ({book.get_condition_display()})\n'
            f'{settings.SITE_URL}/book/{book.pk}/\n\n'
            f'Manage your saved searches: {settings.SITE_URL}/saved-searches/'
        ),
    )


def notify_request_fulfilled(book_request, book):
    """
    Sent to a buyer when a seller lists a book matching their open
    BookRequest - fired from books/views.py:add_book, using
    books/matching.py:find_matching_book_requests to find the request(s).
    """
    dispatch(
        book_request.buyer,
        title=f'"{book_request.title}" is now available!',
        body=f'"{book.title}" - \u20b9{book.price} ({book.get_condition_display()}) matches your request.',
        url=f'/book/{book.pk}/',
        email_body=(
            f'Good news - a seller just listed a book matching your request for '
            f'"{book_request.title}":\n\n'
            f'"{book.title}" - \u20b9{book.price} ({book.get_condition_display()})\n'
            f'{settings.SITE_URL}/book/{book.pk}/\n\n'
            f'Grab it before someone else does.'
        ),
    )


def notify_refund_issued(payment):
    dispatch(
        payment.order.buyer,
        title=f'Refund issued - {payment.order.book.title}',
        body=f'A refund of \u20b9{payment.order.final_price} has been issued to your original payment method.',
        url=f'/orders/order/{payment.order.order_id}/',
        email_body=(
            f'A refund of \u20b9{payment.order.final_price} for "{payment.order.book.title}" has been issued '
            f'to your original payment method. It may take a few business days to reflect.'
        ),
    )


def notify_new_message(message):
    """
    Sent to the *other* participant in a chat conversation - fired from
    chatapp/views.py:conversation_view right after a Message is created.
    SMS/WhatsApp is skipped (allow_phone=False) - a running chat isn't
    worth a text per message; push + in-app cover "you have a new message"
    well on their own.
    """
    convo = message.conversation
    recipient = convo.other_party(message.sender)
    title_text = convo.subject_title
    preview = message.text[:80] if message.text else (
        'Sent a photo' if message.image else 'Shared a location' if message.shared_location else 'New message'
    )
    dispatch(
        recipient,
        title=f'New message from {message.sender.username}',
        body=f'{preview} (re: "{title_text}")',
        url=f'/chat/{convo.pk}/',
        email_body=(
            f'{message.sender.username} sent you a message about "{title_text}":\n\n{preview}\n\n'
            f'Reply here: {settings.SITE_URL}/chat/{convo.pk}/'
        ),
        allow_phone=False,
    )


def notify_listing_approved(book):
    """
    Fired from adminpanel/views.py:approve_book once staff clears a
    listing Fraud Detection had held under review - lets the seller know
    it's actually live instead of them having to keep checking manually.
    """
    dispatch(
        book.seller,
        title=f'Your listing "{book.title}" is live',
        body='Our review is complete and your listing is now visible to buyers.',
        url=f'/book/{book.pk}/',
        email_body=(
            f'Good news - "{book.title}" passed review and is now live on BookBridge, '
            f'visible to buyers.\n\nView it here: {settings.SITE_URL}/book/{book.pk}/'
        ),
    )


def notify_listing_removed(book):
    """
    Fired from adminpanel/views.py:remove_book once staff confirms a
    Fraud Detection flag was correct and takes the listing down.
    """
    dispatch(
        book.seller,
        title=f'Your listing "{book.title}" was removed',
        body='Our review found an issue with this listing, so we\'ve taken it down.',
        url='',
        email_body=(
            f'"{book.title}" has been removed from BookBridge following a review. '
            f'If you believe this was a mistake, please contact support.'
        ),
    )


def notify_price_drop(book, old_price):
    """
    Fired from books/views.py:edit_book when a seller lowers the price on
    a listing that's still visible (not pulled into fraud review by the
    same edit) - tells everyone who has it on their Wishlist
    (books.models.Wishlist), the same way notify_saved_search_match tells
    buyers about new matches.
    """
    for entry in book.wishlisted_by.select_related('user').all():
        dispatch(
            entry.user,
            title=f'Price drop: "{book.title}"',
            body=f'Now \u20b9{book.price}, down from \u20b9{old_price}.',
            url=f'/book/{book.pk}/',
            email_body=(
                f'A book on your wishlist just got cheaper - "{book.title}" is now '
                f'\u20b9{book.price} (was \u20b9{old_price}).\n\n'
                f'View it here: {settings.SITE_URL}/book/{book.pk}/'
            ),
        )


def notify_partner_new_lead(partner, buyback):
    """Tells an approved Shopkeeper Partner a new buyback request has just
    appeared inside their lead radius."""
    from django.urls import reverse
    price = f' (asking \u20b9{buyback.expected_price})' if buyback.expected_price else ''
    dispatch(
        partner.user,
        title=f'New buyback lead: "{buyback.title}"',
        body=f'{buyback.get_condition_display()} condition, {buyback.pickup_city}{price}. Make an offer before another partner does.',
        url=reverse('partners:lead_detail', args=[buyback.pk]),
        allow_phone=False,
    )


def _format_window(minutes):
    return f'{minutes // 60} hour{"s" if minutes // 60 != 1 else ""}' if minutes % 60 == 0 and minutes >= 60 else f'{minutes} minutes'


def notify_partner_lead_reserved(partner, buyback, minutes):
    """A new lead was routed to this partner exclusively for a limited window."""
    from django.urls import reverse
    price = f' (asking \u20b9{buyback.expected_price})' if buyback.expected_price else ''
    dispatch(
        partner.user,
        title=f'Lead reserved for you: "{buyback.title}"',
        body=(f'{buyback.get_condition_display()} condition, {buyback.pickup_city}{price}. '
              f'Make an offer within {_format_window(minutes)} - after that it goes to the next partner nearby.'),
        url=reverse('partners:lead_detail', args=[buyback.pk]),
        allow_phone=False,
    )


def notify_partner_lead_lapsed(partner, buyback):
    """The partner's exclusive window ran out (or they paused/were suspended) and the lead moved on."""
    from django.urls import reverse
    dispatch(
        partner.user,
        title=f'Lead passed on: "{buyback.title}"',
        body='You did not respond in time, so this lead moved to another partner. Respond faster to earn the Quick Responder badge.',
        url=reverse('partners:dashboard'),
        allow_phone=False,
    )


def notify_partner_new_review(review):
    from django.urls import reverse
    dispatch(
        review.partner.user,
        title=f'New {review.rating}-star review',
        body=f'A seller rated your pickup of "{review.buyback_request.title}" {review.rating}/5.',
        url=reverse('partners:profile', args=[review.partner_id]),
        allow_phone=False,
    )


def notify_partner_application_decision(partner):
    """Tells an applicant their Shopkeeper Partner application was approved or
    rejected. Fired from adminpanel/views.py:approve_partner / reject_partner so
    applicants don't have to keep checking the status page."""
    from django.urls import reverse
    if partner.status == 'approved':
        dispatch(
            partner.user,
            title='Your Partner application is approved',
            body=f'"{partner.shop_name}" is now a BookBridge Partner. Open your dashboard to see nearby buyback leads.',
            url=reverse('partners:dashboard'),
            email_body=(
                f'Good news - "{partner.shop_name}" has been approved as a BookBridge Shopkeeper Partner.\n\n'
                f'You can now see buyback leads near your shop and make offers.\n\n'
                f'Open your dashboard: {settings.SITE_URL}{reverse("partners:dashboard")}'
            ),
        )
    elif partner.status == 'rejected':
        reason = partner.rejection_reason.strip()
        reason_line = f'Reason: {reason}' if reason else 'No specific reason was given.'
        dispatch(
            partner.user,
            title='Your Partner application needs changes',
            body=f'We could not approve "{partner.shop_name}". {reason_line} You can fix this and re-apply.',
            url=reverse('partners:apply') + '?reapply=1',
            email_body=(
                f'We were not able to approve "{partner.shop_name}" as a BookBridge Shopkeeper Partner.\n\n'
                f'{reason_line}\n\n'
                f'You can correct your details or documents and re-apply here: '
                f'{settings.SITE_URL}{reverse("partners:apply")}?reapply=1'
            ),
        )


# --- Buyback pickup scheduling + handover OTP ----------------------------

def _other_side(buyback, user):
    """The party on the other end of a buyback from `user` (seller <-> partner)."""
    return buyback.partner.user if user == buyback.seller else buyback.seller


def _buyback_url_for(buyback, user):
    from django.urls import reverse
    if buyback.partner and user == buyback.partner.user:
        return reverse('partners:lead_detail', args=[buyback.pk])
    return reverse('partners:buyback_detail', args=[buyback.pk])


def notify_pickup_proposed(buyback, proposer):
    """Tells the other side a pickup date/slot was proposed and needs confirming."""
    recipient = _other_side(buyback, proposer)
    dispatch(
        recipient,
        title=f'Pickup proposed - {buyback.title}',
        body=f'{proposer.username} proposed {buyback.pickup_summary}. Confirm it or suggest another time.',
        url=_buyback_url_for(buyback, recipient),
        allow_phone=False,
    )


def notify_pickup_confirmed(buyback, confirmer):
    """Fired when the non-proposing side confirms the slot. Tells the proposer,
    and texts the seller the handover OTP (notify_phone, not dispatch(): like
    the delivery OTP it isn't preference-gated - the seller needs it at pickup)."""
    proposer = buyback.pickup_proposed_by
    if proposer:
        dispatch(
            proposer,
            title=f'Pickup confirmed - {buyback.title}',
            body=f'{confirmer.username} confirmed pickup for {buyback.pickup_summary}.',
            url=_buyback_url_for(buyback, proposer),
            allow_phone=False,
        )
    notify_pickup_otp(buyback)


def notify_pickup_otp(buyback):
    if not buyback.pickup_otp:
        return False
    when = f' for {buyback.pickup_summary}' if buyback.has_pickup_schedule else ''
    body = (f'Your BookBridge pickup OTP for "{buyback.title}"{when} is {buyback.pickup_otp}. '
            f'Give it to {buyback.partner.shop_name} only when they collect the book and pay you. '
            f'Do not share it before that.')
    return notify_phone(buyback.seller.phone, body)


# --- Book donation + certificate -----------------------------------------

def notify_donation_submitted(donation):
    """Confirms to the donor that their donation request was received."""
    from django.urls import reverse
    url = reverse('donations:detail', args=[donation.pk])
    dispatch(
        donation.donor,
        title='Donation request received',
        body=f'Thank you! Your donation to {donation.partner.name} is awaiting review.',
        url=url,
        allow_phone=False,
        email_body=(
            f'Thank you for donating books through {settings.SITE_NAME}!\n\n'
            f'Your donation to {donation.partner.name} is awaiting review. We will tell you as soon as it is '
            f'accepted, and your certificate is issued once the books have been received.\n\n'
            f'Track it here: {settings.SITE_URL}{url}'
        ),
    )


def notify_donation_accepted(donation):
    from django.urls import reverse
    url = reverse('donations:detail', args=[donation.pk])
    if donation.handover == 'pickup':
        next_step = 'We will contact you to arrange the pickup.'
    else:
        next_step = f'Please hand the books over at: {donation.partner.address or donation.partner.name}.'
    dispatch(
        donation.donor,
        title='Your donation was accepted',
        body=f'Your donation to {donation.partner.name} was accepted. {next_step}',
        url=url,
        allow_phone=False,
        email_body=(
            f'Good news - your book donation to {donation.partner.name} has been accepted.\n\n{next_step}\n\n'
            f'Your certificate is issued as soon as the books are received and counted.\n'
            f'Details: {settings.SITE_URL}{url}'
        ),
    )


def notify_donation_rejected(donation):
    from django.urls import reverse
    url = reverse('donations:detail', args=[donation.pk])
    dispatch(
        donation.donor,
        title='Your donation could not be accepted',
        body=f'We could not accept your donation to {donation.partner.name}. Reason: {donation.rejection_reason}',
        url=url,
        allow_phone=False,
        email_body=(
            f'We were not able to accept your book donation to {donation.partner.name}.\n\n'
            f'Reason: {donation.rejection_reason}\n\n'
            f'You are welcome to submit a new donation: {settings.SITE_URL}{reverse("donations:create")}'
        ),
    )


def notify_certificate_issued(certificate):
    """In-app/push/email notice plus the PDF certificate attached to the email
    (best-effort like _email_invoice: a failed render/send is logged, never raised)."""
    from django.urls import reverse
    from notifications.models import NotificationPreference
    from donations.certificate import render_certificate_pdf, verify_url

    donor = certificate.donation.donor
    url = reverse('donations:detail', args=[certificate.donation_id])
    books = f'{certificate.books_count} book' + ('' if certificate.books_count == 1 else 's')
    dispatch(
        donor,
        title='Your donation certificate is ready',
        body=f'Thank you for donating {books} to {certificate.partner_name}. Download your certificate.',
        url=url,
        allow_phone=False,
        send_email_channel=False,  # one email with the PDF attached is sent below instead
    )
    if NotificationPreference.for_user(donor).email_enabled:
        body = (
            f'Thank you for donating {books} to {certificate.partner_name}!\n\n'
            f'Certificate No: {certificate.code}\n'
            f'Download or share it: {settings.SITE_URL}{url}\n'
            f'Anyone can verify it at: {verify_url(certificate)}'
        )
        try:
            pdf_bytes = render_certificate_pdf(certificate)
        except Exception:
            logger.exception('Failed to render certificate PDF %s', certificate.code)
            send_email(donor.email, 'Your donation certificate', body)  # still tell them, just without the PDF
            return
        send_email_with_attachment(
            donor.email, 'Your donation certificate', body + '\n\nThe PDF certificate is attached.',
            attachment_bytes=pdf_bytes, attachment_filename=f'donation-certificate-{certificate.code}.pdf',
            mimetype='application/pdf',
        )


def notify_certificate_revoked(certificate):
    from django.urls import reverse
    dispatch(
        certificate.donation.donor,
        title='Your donation certificate was revoked',
        body=f'Certificate {certificate.code} was revoked. Reason: {certificate.revoked_reason}',
        url=reverse('donations:detail', args=[certificate.donation_id]),
        allow_phone=False,
    )


def notify_payouts_paid(seller, payouts):
    """One message per UPI transfer, however many orders it settled."""
    from decimal import Decimal
    from payouts.upi import mask_upi_id
    total = sum((p.net_amount for p in payouts), Decimal('0.00'))
    first = payouts[0]
    orders = ', '.join(f'#{p.order_ref}' for p in payouts[:5]) + (f' and {len(payouts) - 5} more' if len(payouts) > 5 else '')
    dispatch(
        seller,
        title=f'Payout of \u20b9{total} sent',
        body=f'\u20b9{total} was sent to {mask_upi_id(first.paid_to_upi)} (UTR {first.utr}).',
        url='/payouts/',
        email_body=(
            f'We have sent \u20b9{total} to your UPI ID {mask_upi_id(first.paid_to_upi)}.\n'
            f'UTR / reference: {first.utr}\nOrders: {orders}\n\n'
            f'You can see every payout and download your commission statement from the Payouts page.'
        ),
    )


def notify_upi_changed(user, old_upi, new_upi):
    """Security notice, so it goes by email regardless of notification
    preferences (like the OTP emails), plus the in-app notification."""
    from payouts.upi import mask_upi_id
    old = mask_upi_id(old_upi) or 'none'
    body = (
        f'The payout UPI ID on your {settings.SITE_NAME} account was changed from {old} to {mask_upi_id(new_upi)}. '
        f'If this was not you, change your password and contact {settings.DEFAULT_FROM_EMAIL} straight away.'
    )
    dispatch(
        user, title='Your payout UPI ID was changed', body=body, url='/payouts/', allow_phone=False,
        send_email_channel=False,
    )
    send_email(user.email, 'Your payout UPI ID was changed', body)


# --- Buyer disputes -----------------------------------------------------------

def _dispute_url(dispute):
    return f'/disputes/{dispute.pk}/'


def notify_dispute_opened(dispute):
    dispatch(
        dispute.seller,
        title=f'Dispute opened - {dispute.book_title}',
        body=f'{dispute.buyer.username} opened dispute {dispute.reference}: {dispute.get_category_display()}. Please reply.',
        url=_dispute_url(dispute),
        email_body=(
            f'{dispute.buyer.username} opened dispute {dispute.reference} on order #{dispute.order_ref} '
            f'(\"{dispute.book_title}\") - {dispute.get_category_display()}.\n\n{dispute.description}\n\n'
            f'Please reply before {dispute.seller_response_due:%d %b %Y}; if you do not, the dispute goes to the '
            f'BookBridge team. Your payout for this order is on hold until it is settled.'
        ),
    )
    dispatch(
        dispute.buyer,
        title=f'Dispute {dispute.reference} opened',
        body=f'We told {dispute.seller.username}. They have until {dispute.seller_response_due:%d %b} to reply.',
        url=_dispute_url(dispute), allow_phone=False, send_email_channel=False,
    )


def notify_dispute_message(dispute, message):
    """Tell whoever did not write the message. Staff replies go to both sides."""
    recipients = {
        'buyer': [dispute.seller], 'seller': [dispute.buyer], 'staff': [dispute.buyer, dispute.seller],
    }.get(message.role, [])
    who = 'BookBridge' if message.role == 'staff' else (message.author.username if message.author else 'Someone')
    for user in recipients:
        dispatch(
            user,
            title=f'New reply on dispute {dispute.reference}',
            body=f'{who}: {message.body[:120]}',
            url=_dispute_url(dispute), allow_phone=False,
            email_body=f'{who} replied on dispute {dispute.reference} (\"{dispute.book_title}\"):\n\n{message.body}',
        )


def notify_dispute_escalated(dispute):
    if dispute.auto_escalated:
        why_buyer = f'{dispute.seller.username} did not reply in time, so the BookBridge team will review it.'
        why_seller = 'You did not reply in time, so the BookBridge team will now decide.'
    elif dispute.seller_agreed:
        why_buyer = f'{dispute.seller.username} agreed you should be refunded. The BookBridge team will issue it.'
        why_seller = 'Thanks for agreeing to the refund. The BookBridge team will issue it.'
    else:
        why_buyer = 'The BookBridge team will review it and decide.'
        why_seller = f'{dispute.buyer.username} escalated it. The BookBridge team will review and decide.'
    dispatch(dispute.buyer, title=f'Dispute {dispute.reference} escalated', body=why_buyer, url=_dispute_url(dispute), allow_phone=False)
    dispatch(dispute.seller, title=f'Dispute {dispute.reference} escalated', body=why_seller, url=_dispute_url(dispute), allow_phone=False)


def notify_dispute_withdrawn(dispute):
    dispatch(
        dispute.seller,
        title=f'Dispute {dispute.reference} withdrawn',
        body=f'{dispute.buyer.username} withdrew the dispute. Your payout is no longer held for it.',
        url=_dispute_url(dispute), allow_phone=False,
    )


def notify_dispute_resolved(dispute):
    outcome = dispute.get_outcome_display()
    detail = ''
    if dispute.outcome == 'refund' and dispute.refund_amount:
        detail = f' \u20b9{dispute.refund_amount} was refunded to the {dispute.get_refund_destination_display()}.'
    for user, title in ((dispute.buyer, f'Dispute {dispute.reference} resolved'), (dispute.seller, f'Dispute {dispute.reference} resolved')):
        dispatch(
            user,
            title=title,
            body=f'{outcome}.{detail}',
            url=_dispute_url(dispute),
            email_body=(
                f'Dispute {dispute.reference} on order #{dispute.order_ref} (\"{dispute.book_title}\") has been resolved.\n\n'
                f'Outcome: {outcome}.{detail}\n\nFrom the BookBridge team:\n{dispute.resolution_note}'
            ),
        )


# --- Account security ----------------------------------------------------------

def notify_password_changed(user):
    """Security notice, emailed regardless of notification preferences (like the OTP
    and UPI-change emails), plus the in-app notification."""
    body = (
        f'The password on your {settings.SITE_NAME} account was just changed. '
        f'If this was not you, call {settings.SUPPORT_PHONE} or email {settings.SUPPORT_EMAIL} straight away.'
    )
    dispatch(
        user, title='Your password was changed', body=body, url='/accounts/profile/', allow_phone=False,
        send_email_channel=False,
    )
    send_email(user.email, 'Your password was changed', body)
