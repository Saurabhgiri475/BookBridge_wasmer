from django.conf import settings
from django.utils.translation import gettext as _
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404

from accounts import throttle
from accounts.validators import clean_proof_image
from books.models import Book
from bookmarket.notify import notify_new_message
from bookmarket.private_files import private_image_response
from partners.models import BuybackRequest, ShopkeeperPartner
from .models import Conversation, Message

CHAT_MAX_MESSAGES_PER_HOUR = 120
CHAT_MAX_IMAGES_PER_HOUR = 10
CHAT_MAX_TEXT_CHARS = 2000
CHAT_MAX_LOCATION_CHARS = 300


@login_required
def start_conversation(request, book_pk):
    book = get_object_or_404(Book, pk=book_pk)
    if book.seller == request.user:
        return redirect('chatapp:inbox')
    convo, _created = Conversation.objects.get_or_create(book=book, buyer=request.user, seller=book.seller)
    return redirect('chatapp:conversation', pk=convo.pk)


@login_required
def start_buyback_conversation(request, buyback_pk):
    """Seller <-> Shopkeeper Partner chat about one buyback lead.

    The seller can open it once a partner is attached (they made an offer);
    an approved partner can open it on an open lead (to ask questions before
    offering) or on a lead assigned to them. Nobody else can - 404."""
    buyback = get_object_or_404(BuybackRequest.objects.select_related('partner__user', 'seller'), pk=buyback_pk)
    if buyback.status == 'cancelled':
        raise Http404

    if request.user == buyback.seller:
        if not buyback.partner_id:
            django_messages.info(request, 'No partner has made an offer yet - you can chat once one does.')
            return redirect('partners:buyback_detail', pk=buyback.pk)
        partner_user = buyback.partner.user
    else:
        partner = ShopkeeperPartner.objects.filter(user=request.user, status='approved').first()
        if not partner or not (buyback.status == 'pending' or buyback.partner_id == partner.pk):
            raise Http404
        partner_user = request.user

    convo, _created = Conversation.objects.get_or_create(buyback=buyback, buyer=partner_user, seller=buyback.seller)
    return redirect('chatapp:conversation', pk=convo.pk)


@login_required
def inbox(request):
    conversations = Conversation.objects.filter(
        buyer=request.user
    ) | Conversation.objects.filter(seller=request.user)
    conversations = conversations.distinct().select_related('book', 'buyback', 'buyer', 'seller')
    return render(request, 'chatapp/inbox.html', {'conversations': conversations})


@login_required
def conversation_view(request, pk):
    convo = get_object_or_404(Conversation.objects.select_related('book', 'buyback', 'buyer', 'seller'), pk=pk)
    if request.user not in (convo.buyer, convo.seller):
        return redirect('chatapp:inbox')

    if request.method == 'POST':
        text = request.POST.get('text', '').strip()
        location = request.POST.get('location', '').strip()
        image = request.FILES.get('image')

        # Abuse limits: message flood per user, tighter cap on image uploads,
        # bounded text, and real image validation (size, pixels, actual
        # decodable JPEG/PNG/WebP/HEIC; re-encoded, EXIF/GPS stripped).
        blocked = None
        if settings.RATE_LIMITING_ENABLED:
            if not throttle.hit('chat-msg', request.user.pk, CHAT_MAX_MESSAGES_PER_HOUR, 3600)[0]:
                blocked = 'You are sending messages too fast. Please slow down and try again shortly.'
            elif image and not throttle.hit('chat-img', request.user.pk, CHAT_MAX_IMAGES_PER_HOUR, 3600)[0]:
                blocked = f'You can send up to {CHAT_MAX_IMAGES_PER_HOUR} photos per hour. Please try again later.'
        if not blocked and len(text) > CHAT_MAX_TEXT_CHARS:
            blocked = f'Messages can be up to {CHAT_MAX_TEXT_CHARS} characters.'
        if not blocked and len(location) > CHAT_MAX_LOCATION_CHARS:
            blocked = 'That location text is too long.'
        if not blocked and image:
            try:
                image = clean_proof_image(image, label='photo')
            except ValidationError as exc:
                blocked = exc.messages[0]
        if blocked:
            django_messages.error(request, blocked)
            return redirect('chatapp:conversation', pk=pk)

        if text or location or image:
            message = Message.objects.create(
                conversation=convo, sender=request.user, text=text,
                shared_location=location, image=image,
            )
            notify_new_message(message)
        return redirect('chatapp:conversation', pk=pk)

    convo.messages.exclude(sender=request.user).update(is_read=True)
    return render(request, 'chatapp/conversation.html', {'convo': convo, 'messages': convo.messages.all()})


@login_required
def chat_image(request, pk):
    """The only way to read a chat photo: the two people in the conversation. Anyone
    else (including other logged-in users and staff) gets a 404, so the URL does not
    even confirm that the photo exists."""
    message = get_object_or_404(Message.objects.select_related('conversation'), pk=pk)
    convo = message.conversation
    if request.user.pk not in (convo.buyer_id, convo.seller_id) or not message.image:
        raise Http404
    return private_image_response(message.image)
