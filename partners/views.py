import csv
import json
import os
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from bookmarket.security import safe_next_url
from django.utils import timezone
from django.utils.translation import gettext as _, ngettext
from django.views.decorators.http import require_POST

import mimetypes

from accounts import twofactor
from adminpanel.models import AuditLog
from payouts.statements import period_choices
from books.models import Book, BookImage
from books.price_recommendation import recommend_price
from orders.models import Order
from wallet.services import InsufficientBalance, debit as wallet_debit

from .forms import (
    PartnerReviewForm, BuybackRequestForm, BuybackRequestImageFormSet, PartnerApplicationForm, PartnerCompleteForm, PartnerOfferForm,
    PartnerSettingsForm, PickupScheduleForm,
)
from . import assignment, badges
from .models import MAX_PICKUP_OTP_ATTEMPTS, BuybackRequest, BuybackRequestImage, PartnerCommission, PartnerReview, ShopkeeperPartner
from .utils import DEFAULT_MATCH_RADIUS_KM, estimate_buyback_quote, nearby_open_requests


def _partner_or_none(user):
    return ShopkeeperPartner.objects.filter(user=user).first()


# --- Becoming a partner --------------------------------------------------

_INLINE_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


@login_required
def proof_file(request, pk, kind):
    """
    The only way to read a partner's ID / shop proof. Allowed: the applicant
    themselves (to see their own saved upload) and staff. Anyone else gets a
    404, so the URL doesn't even confirm that a document exists. Staff opening
    someone else's document is written to the audit log.
    """
    if kind not in ('id', 'shop'):
        raise Http404
    partner = get_object_or_404(ShopkeeperPartner, pk=pk)
    is_owner = partner.user_id == request.user.pk
    staff = twofactor.staff_ok(request, 'cap_partners')  # staff must have passed 2FA to open someone else's documents
    if not (is_owner or staff):
        raise Http404
    field = partner.owner_id_proof if kind == 'id' else partner.shop_proof
    if not field:
        raise Http404
    try:
        handle = field.open('rb')
    except (FileNotFoundError, ValueError):
        raise Http404

    if not is_owner:
        AuditLog.record(
            actor=request.user, action='view_partner_proof',
            target_repr=f'{partner.shop_name} ({"ID" if kind == "id" else "shop"} proof)', target_id=partner.pk,
            request=request,
        )

    content_type = mimetypes.guess_type(field.name)[0] or 'application/octet-stream'
    inline = content_type in _INLINE_IMAGE_TYPES
    response = FileResponse(handle, content_type=content_type if inline else 'application/octet-stream',
                            as_attachment=not inline, filename=os.path.basename(field.name))
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response

def _delete_replaced_files(partner, old_names):
    """After documents were replaced, remove the superseded files from storage so
    old ID photos don't pile up (they are personal documents - keep as few as possible)."""
    for field, old_name in old_names.items():
        new_name = getattr(partner, field).name
        if old_name and old_name != new_name:
            try:
                partner._meta.get_field(field).storage.delete(old_name)
            except Exception:
                pass  # a leftover file is harmless; never fail the applicant's save over it


@login_required
def apply(request):
    existing = _partner_or_none(request.user)

    # A rejected applicant can fix their details/documents and re-apply on the
    # same record (?reapply=1, or the POST that follows). A *pending* applicant can
    # correct details or swap unclear documents before review (?edit=1, or the POST
    # that follows). Approved and suspended applications only ever see the status page.
    reapplying = bool(
        existing and existing.status == 'rejected'
        and (request.method == 'POST' or request.GET.get('reapply'))
    )
    editing = bool(
        existing and existing.status == 'pending'
        and (request.method == 'POST' or request.GET.get('edit'))
    )
    if existing and not reapplying and not editing:
        return render(request, 'partners/application_status.html', {'partner': existing})

    if request.method == 'POST':
        with transaction.atomic():
            if existing:
                # Lock the row and re-check: staff may have approved/rejected it while this form was open.
                existing = ShopkeeperPartner.objects.select_for_update().get(pk=existing.pk)
                if editing and existing.status != 'pending':
                    messages.info(request, _('Your application was reviewed while you were editing - see its current status below.'))
                    return redirect('partners:apply')
            old_names = {
                'owner_id_proof': existing.owner_id_proof.name if existing else '',
                'shop_proof': existing.shop_proof.name if existing else '',
            }
            form = PartnerApplicationForm(
                request.POST, request.FILES, instance=existing if (reapplying or editing) else None, editing=editing,
            )
            if form.is_valid():
                partner = form.save(commit=False)
                partner.user = request.user
                if reapplying:
                    partner.status = 'pending'
                    partner.rejection_reason = ''
                    partner.reviewed_at = None
                    partner.reviewed_by = None
                if editing or reapplying:
                    partner.last_edited_at = timezone.now()
                partner.save()
                if editing or reapplying:
                    _delete_replaced_files(partner, old_names)
                if editing:
                    messages.success(request, _('Your application was updated. Our team will review the latest version.'))
                elif reapplying:
                    messages.success(request, _('Application re-submitted! Our team will review it again shortly.'))
                else:
                    messages.success(request, _('Application submitted! Our team will review your documents and get back to you shortly.'))
                return redirect('partners:apply')
    else:
        form = PartnerApplicationForm(instance=existing if (reapplying or editing) else None, editing=editing)
    return render(request, 'partners/apply.html', {
        'form': form, 'reapplying': reapplying, 'editing': editing,
        'previous': existing if reapplying else None, 'partner': existing if (editing or reapplying) else None,
    })


# --- Partner dashboard -----------------------------------------------------

def _partner_gate(request):
    """Returns (partner, None) for an approved partner, else (None, redirect)."""
    partner = _partner_or_none(request.user)
    if not partner:
        messages.info(request, _('You need to apply and be approved as a Shopkeeper Partner first.'))
        return None, redirect('partners:apply')
    if not partner.is_approved:
        return None, render(request, 'partners/application_status.html', {'partner': partner})
    return partner, None


@login_required
def dashboard(request):
    partner, early = _partner_gate(request)
    if early:
        return early

    try:
        radius_km = int(request.GET.get('radius', partner.default_radius_km))
    except ValueError:
        radius_km = partner.default_radius_km
    radius_km = max(1, min(radius_km, 200))  # keep it sane - no accidental radius=99999 scans

    # Cheap safety net so lapsed leads still move on if the cron job isn't set up yet.
    try:
        assignment.sweep_expired()
    except Exception:
        pass

    open_requests = []
    if partner.is_accepting_requests:
        # Leads reserved for another partner stay hidden until they lapse or are passed on.
        open_requests_qs = BuybackRequest.objects.filter(status='pending').exclude(seller=request.user).filter(
            Q(assigned_partner__isnull=True) | Q(assigned_partner=partner),
        )
        open_requests = nearby_open_requests(partner, open_requests_qs, radius_km=radius_km)
        for lead in open_requests:
            lead.reserved_for_me = lead.assigned_partner_id == partner.pk
            lead.time_left = assignment.time_left_label(lead) if lead.reserved_for_me else ''
        # Reserved-for-me leads first, then by distance (the list is already nearest-first).
        open_requests.sort(key=lambda lead: not lead.reserved_for_me)
    my_requests = BuybackRequest.objects.filter(
        partner=partner, status__in=['offer_made', 'accepted'],
    ).order_by('-updated_at')
    completed = BuybackRequest.objects.filter(partner=partner, status='paid').order_by('-completed_at')[:10]
    commissions_due = partner.commissions.filter(is_settled=False).select_related('buyback_request')

    now = timezone.now()
    month_qs = BuybackRequest.objects.filter(
        partner=partner, status='paid', completed_at__year=now.year, completed_at__month=now.month,
    )
    month_spend = sum((b.final_price or 0) for b in month_qs)

    context = {
        'partner': partner,
        'open_requests': open_requests,
        'my_requests': my_requests,
        'completed': completed,
        'commissions_due': commissions_due,
        'total_due': partner.outstanding_commission(),
        'radius_km': radius_km,
        'has_shop_coords': partner.shop_latitude is not None and partner.shop_longitude is not None,
        'month_count': month_qs.count(),
        'month_spend': month_spend,
        'stock_count': Book.objects.filter(seller=request.user, status__in=['available', 'reserved']).count(),
        'reputation': badges.reputation(partner),
        'reserved_count': sum(1 for lead in open_requests if lead.reserved_for_me),
        'response_minutes': assignment.response_minutes(),
    }
    return render(request, 'partners/dashboard.html', context)


@require_POST
@login_required
def toggle_accepting(request):
    partner, early = _partner_gate(request)
    if early:
        return early
    partner.is_accepting_requests = not partner.is_accepting_requests
    partner.save(update_fields=['is_accepting_requests'])
    messages.success(
        request,
        _('Your shop is now open for new buyback leads.') if partner.is_accepting_requests
        else _('Lead intake paused - you will not see or be alerted about new requests until you reopen.'),
    )
    return redirect(safe_next_url(request, 'partners:dashboard'))


def _require_approved_partner(user):
    partner = _partner_or_none(user)
    return partner if partner and partner.is_approved else None


@require_POST
@login_required
def make_offer(request, pk):
    partner = _require_approved_partner(request.user)
    if not partner:
        messages.error(request, _('Only approved partners can make offers.'))
        return redirect('partners:apply')

    form = PartnerOfferForm(request.POST)
    with transaction.atomic():
        buyback = get_object_or_404(BuybackRequest.objects.select_for_update().exclude(seller=request.user), pk=pk, status='pending')
        if not buyback.visible_to(partner):
            messages.error(request, _('This lead is currently reserved for another partner. It opens up if they don\u2019t respond in time.'))
            return redirect('partners:dashboard')
        if form.is_valid():
            buyback.partner = partner
            buyback.offered_price = form.cleaned_data['offered_price']
            buyback.offer_note = form.cleaned_data['offer_note']
            buyback.status = 'offer_made'
            buyback.offered_at = timezone.now()
            buyback.assignment_expires_at = None
            buyback.save(update_fields=['partner', 'offered_price', 'offer_note', 'status', 'offered_at', 'assignment_expires_at', 'updated_at'])
            messages.success(request, _('Offer of ₹%(offered_price)s sent for "%(title)s".') % {'offered_price': buyback.offered_price, 'title': buyback.title})
        else:
            messages.error(request, _('Enter a valid offer amount.'))
    return redirect('partners:dashboard')


@require_POST
@login_required
def pass_lead(request, pk):
    """The partner a lead is reserved for declines it, so it moves on straight away."""
    partner = _require_approved_partner(request.user)
    if not partner:
        messages.error(request, _('Only approved partners can do this.'))
        return redirect('partners:apply')
    if assignment.pass_lead(pk, partner):
        messages.info(request, _('Passed - the lead has moved on to the next partner nearby.'))
    else:
        messages.error(request, _('That lead isn\u2019t reserved for you any more.'))
    return redirect('partners:dashboard')


@require_POST
@login_required
def mark_paid(request, pk):
    """Partner confirms they've collected the book and paid the seller -
    this is what creates the commission owed to BookBridge."""
    partner = _require_approved_partner(request.user)
    if not partner:
        messages.error(request, _('Only approved partners can do this.'))
        return redirect('partners:apply')

    buyback = get_object_or_404(BuybackRequest, pk=pk, partner=partner, status='accepted')
    form = PartnerCompleteForm(request.POST)
    if form.is_valid():
        # Handover OTP: proves the seller was there and agreed to hand over the
        # book. Requests accepted before OTPs existed have none and skip this.
        if buyback.pickup_otp:
            submitted = form.cleaned_data['pickup_otp'].strip()
            if buyback.pickup_otp_locked:
                messages.error(
                    request,
                    _('Too many wrong OTP attempts. Ask the seller to reissue the pickup OTP from their request page.'),
                )
                return redirect('partners:dashboard')
            if not submitted:
                messages.error(request, _('Enter the seller\u2019s 6-digit pickup OTP to complete this pickup.'))
                return redirect('partners:dashboard')
            if not buyback.check_pickup_otp(submitted):
                left = max(0, MAX_PICKUP_OTP_ATTEMPTS - buyback.pickup_otp_attempts)
                messages.error(request, ngettext('Incorrect pickup OTP. %(left)d attempt left.', 'Incorrect pickup OTP. %(left)d attempts left.', left) % {'left': left})
                return redirect('partners:dashboard')
        final_price = form.cleaned_data['final_price']
        list_for_resale = form.cleaned_data['list_for_resale']
        resale_price = form.cleaned_data['resale_price']
        with transaction.atomic():
            buyback.final_price = final_price
            buyback.status = 'paid'
            buyback.completed_at = timezone.now()
            update_fields = ['final_price', 'status', 'completed_at', 'updated_at']
            if list_for_resale:
                buyback.listed_book = _relist_buyback_as_book(buyback, partner, resale_price)
                update_fields.append('listed_book')
            buyback.save(update_fields=update_fields)
            PartnerCommission.objects.create(
                partner=partner,
                buyback_request=buyback,
                amount=PartnerCommission.compute_amount(final_price, partner.commission_percent),
            )
        note = f' and relisted at \u20b9{resale_price}' if list_for_resale else ''
        messages.success(
            request,
            _('"%(title)s" marked as picked up and paid%(note)s. %(commission_percent)s%% commission has been added to your dues.') % {'title': buyback.title, 'note': note, 'commission_percent': partner.commission_percent},
        )
    else:
        messages.error(request, _('Enter the amount you actually paid the seller, and a resale price if relisting.'))
    return redirect('partners:dashboard')


def _relist_buyback_as_book(buyback, partner, resale_price):
    """Publishes the book the partner just bought back as a fresh listing
    under the partner's own account, copying over its details and photos.
    Skipped through fraud_detection - the partner is already a vetted,
    approved account, unlike an anonymous new seller."""
    book = Book.objects.create(
        seller=partner.user,
        category=buyback.category,
        title=buyback.title,
        author=buyback.author,
        publisher=buyback.publisher,
        isbn=buyback.isbn,
        edition=buyback.edition,
        publication_year=buyback.publication_year,
        subject=buyback.subject,
        board=buyback.board,
        university=buyback.university,
        branch=buyback.branch,
        semester=buyback.semester,
        language=buyback.language,
        condition=buyback.condition,
        original_price=buyback.original_price,
        price=resale_price,
        quantity=buyback.quantity,
        description=buyback.description or f'Bought back from a BookBridge seller via {partner.shop_name}.',
        pickup_address=partner.shop_address,
        pickup_city=partner.shop_city,
        pickup_pincode=partner.shop_pincode,
        pickup_latitude=partner.shop_latitude,
        pickup_longitude=partner.shop_longitude,
        status='available',
    )
    for source_image in buyback.images.all():
        book_image = BookImage(book=book, angle=source_image.angle)
        source_image.image.open('rb')
        book_image.image.save(os.path.basename(source_image.image.name), ContentFile(source_image.image.read()), save=True)
        source_image.image.close()
    return book


@require_POST
@login_required
def settle_commission(request, pk):
    partner = _require_approved_partner(request.user)
    if not partner:
        messages.error(request, _('Only approved partners can do this.'))
        return redirect('partners:apply')

    commission = get_object_or_404(PartnerCommission, pk=pk, partner=partner, is_settled=False)
    try:
        # A zero commission has nothing to charge (wallet_debit rejects amounts <= 0).
        if commission.amount > 0:
            wallet_debit(
                request.user, commission.amount, 'buyback_commission',
                description=f'Commission for "{commission.buyback_request.title}"',
                idempotency_key=f'commission:{commission.pk}:{int(commission.created_at.timestamp())}',  # a double submit can't charge it twice
            )
    except InsufficientBalance:
        messages.error(
            request,
            _('Your wallet balance is too low to cover ₹%(amount)s. Add funds to your wallet and try again.') % {'amount': commission.amount},
        )
        return redirect('partners:dashboard')

    commission.is_settled = True
    commission.settled_at = timezone.now()
    commission.save(update_fields=['is_settled', 'settled_at'])
    messages.success(request, _('₹%(amount)s commission settled.') % {'amount': commission.amount})
    return redirect('partners:dashboard')


# --- Seller side: selling straight to BookBridge --------------------------

ACTIVE_BUYBACK_STATUSES = ['pending', 'offer_made', 'accepted']


def _find_duplicate_buybacks(seller, *, title='', isbn='', author=''):
    """Same idea as books.duplicate_check.find_duplicate_listings, but for
    the seller's own still-open buyback requests."""
    title, isbn, author = (title or '').strip(), (isbn or '').strip(), (author or '').strip()
    if not title and not isbn:
        return BuybackRequest.objects.none()
    qs = BuybackRequest.objects.filter(seller=seller, status__in=ACTIVE_BUYBACK_STATUSES)
    if isbn:
        by_isbn = qs.filter(isbn=isbn)
        if by_isbn.exists():
            return by_isbn
    if title:
        by_title = qs.filter(title__iexact=title)
        if author:
            narrowed = by_title.filter(author__iexact=author)
            if narrowed.exists():
                return narrowed
        return by_title
    return qs.none()


@require_POST
@login_required
def check_duplicate(request):
    """Live duplicate check for the Sell to BookBridge form (AJAX)."""
    dupes = _find_duplicate_buybacks(
        request.user,
        title=request.POST.get('title', ''),
        isbn=request.POST.get('isbn', ''),
        author=request.POST.get('author', ''),
    )
    return JsonResponse({
        'duplicate': dupes.exists(),
        'listings': [
            {'id': b.pk, 'title': b.title, 'url': b.get_absolute_url(), 'status': b.get_status_display()}
            for b in dupes[:5]
        ],
    })


@login_required
def sell_to_bookbridge(request):
    own_partner = _partner_or_none(request.user)
    if own_partner and own_partner.is_approved:
        messages.info(
            request,
            _('Partners buy books from sellers rather than selling to BookBridge - '
            'browse open buyback leads on your Partner Dashboard.'),
        )
        return redirect('partners:dashboard')

    if not request.user.is_seller_verified():
        messages.warning(
            request,
            _('Verify your email before selling a book to BookBridge.'),
        )
        return redirect('accounts:verify_seller')

    duplicates = []
    if request.method == 'POST':
        form = BuybackRequestForm(request.POST)
        formset = BuybackRequestImageFormSet(request.POST, request.FILES, instance=BuybackRequest())
        if form.is_valid():
            # Server-side backstop for the live AJAX check - skipped only if
            # the seller saw the warning and clicked "Submit Anyway".
            if request.POST.get('confirm_duplicate') != '1':
                duplicates = list(_find_duplicate_buybacks(
                    request.user,
                    title=form.cleaned_data.get('title', ''),
                    isbn=form.cleaned_data.get('isbn', ''),
                    author=form.cleaned_data.get('author', ''),
                ))
            if duplicates:
                messages.warning(
                    request,
                    _('Already requested - you already have an open buyback request that looks like this book. '
                    'Review it below, or submit anyway if this is a different copy.'),
                )
            else:
                buyback = form.save(commit=False)
                buyback.seller = request.user
                buyback.save()
                formset = BuybackRequestImageFormSet(request.POST, request.FILES, instance=buyback)
                if formset.is_valid():
                    formset.save()
                try:
                    assignment.route_new_lead(buyback)
                except Exception:  # routing/notification trouble must never lose the seller's submission
                    pass
                messages.success(
                    request,
                    _('Your book has been listed for direct buyback - nearby partners will make you an offer soon.'),
                )
                return redirect('partners:buyback_detail', pk=buyback.pk)
    else:
        form = BuybackRequestForm()
        formset = BuybackRequestImageFormSet(instance=BuybackRequest())
    return render(request, 'partners/sell_to_bookbridge.html', {
        'form': form, 'formset': formset, 'duplicates': duplicates,
    })


@require_POST
@login_required
def buyback_quote(request):
    """Instant Buyback Quote for the sell form (AJAX): what partners typically
    pay for this book, before the seller submits anything. See
    partners/utils.py:estimate_buyback_quote()."""
    def _dec(raw):
        try:
            return Decimal(raw) if raw not in (None, '') else None
        except (ArithmeticError, TypeError, ValueError):
            return None

    def _int(raw):
        try:
            return int(raw) if raw not in (None, '') else None
        except (TypeError, ValueError):
            return None

    title = request.POST.get('title', '').strip()
    if not title:
        return JsonResponse({'error': 'Enter a title first so we can find comparable books.'}, status=400)

    quantity = min(max(_int(request.POST.get('quantity')) or 1, 1), 50)
    q = estimate_buyback_quote(
        title=title,
        author=request.POST.get('author', ''),
        isbn=request.POST.get('isbn', ''),
        edition=request.POST.get('edition', ''),
        condition=request.POST.get('condition', 'good'),
        publication_year=_int(request.POST.get('publication_year')),
        original_price=_dec(request.POST.get('original_price')),
        category_id=_int(request.POST.get('category')),
        subject=request.POST.get('subject', ''),
        quantity=quantity,
        city=request.POST.get('pickup_city', ''),
        expected_price=_dec(request.POST.get('expected_price')),
    )
    return JsonResponse({k: (str(v) if isinstance(v, Decimal) else v) for k, v in q.items()})


@login_required
def buyback_list(request):
    requests_qs = BuybackRequest.objects.filter(seller=request.user).order_by('-created_at')
    return render(request, 'partners/buyback_list.html', {'buyback_requests': requests_qs})


@login_required
def buyback_detail(request, pk):
    buyback = get_object_or_404(BuybackRequest.objects.select_related('partner'), pk=pk, seller=request.user)
    review = PartnerReview.objects.filter(buyback_request=buyback).first()
    partner_rep = badges.reputation(buyback.partner) if buyback.partner and buyback.status in ('offer_made', 'accepted', 'paid') else None
    return render(request, 'partners/buyback_detail.html', {
        'buyback': buyback, 'pickup_form': PickupScheduleForm(), 'max_otp_attempts': MAX_PICKUP_OTP_ATTEMPTS,
        'review': review, 'review_form': PartnerReviewForm() if buyback.status == 'paid' and buyback.partner and not review else None,
        'partner_rep': partner_rep,
        'routing_round': buyback.assignment_round, 'reserved': buyback.status == 'pending' and buyback.assigned_partner_id is not None,
    })


@require_POST
@login_required
def review_partner(request, pk):
    """The seller rates the partner after a completed buyback. One review per buyback; cannot be
    edited afterwards (so a rating can't be quietly changed after a dispute)."""
    buyback = get_object_or_404(BuybackRequest, pk=pk, seller=request.user, status='paid', partner__isnull=False)
    if PartnerReview.objects.filter(buyback_request=buyback).exists():
        messages.info(request, _('You have already reviewed this pickup.'))
        return redirect('partners:buyback_detail', pk=pk)
    form = PartnerReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, _('Choose a rating from 1 to 5 stars.'))
        return redirect('partners:buyback_detail', pk=pk)
    from django.db import IntegrityError
    try:
        with transaction.atomic():
            review = PartnerReview.objects.create(
                partner=buyback.partner, buyback_request=buyback, reviewer=request.user,
                rating=int(form.cleaned_data['rating']), comment=form.cleaned_data['comment'].strip(),
            )
    except IntegrityError:  # double-submit race
        return redirect('partners:buyback_detail', pk=pk)
    try:
        from bookmarket.notify import notify_partner_new_review
        notify_partner_new_review(review)
    except Exception:
        pass
    messages.success(request, _('Thanks - your review helps other sellers choose a partner.'))
    return redirect('partners:buyback_detail', pk=pk)


@login_required
def partner_profile(request, pk):
    """What other members can see about an approved partner: shop name, city, rating, badges and
    recent reviews. No contact details, address or documents."""
    partner = get_object_or_404(ShopkeeperPartner, pk=pk, status='approved')
    reviews = list(partner.reviews.filter(is_hidden=False).select_related('reviewer')[:10])
    for r in reviews:
        name = r.reviewer.username
        r.reviewer_label = f'{name[:1]}{"*" * min(max(len(name) - 1, 2), 5)}'
    return render(request, 'partners/profile.html', {
        'shop': partner, 'reputation': badges.reputation(partner), 'reviews': reviews,
        'is_owner': partner.user_id == request.user.pk,
    })


@require_POST
@login_required
def accept_offer(request, pk):
    buyback = get_object_or_404(BuybackRequest, pk=pk, seller=request.user, status='offer_made')
    buyback.status = 'accepted'
    buyback.save(update_fields=['status', 'updated_at'])
    buyback.issue_pickup_otp()
    messages.success(
        request,
        _('Offer accepted - schedule a pickup time with %(shop_name)s below. Your pickup OTP is on this page; share it only at handover.') % {'shop_name': buyback.partner.shop_name},
    )
    return redirect('partners:buyback_detail', pk=pk)


@require_POST
@login_required
def reject_offer(request, pk):
    buyback = get_object_or_404(BuybackRequest, pk=pk, seller=request.user, status='offer_made')
    rejected = buyback.partner
    buyback.reopen()
    try:
        assignment.restart_after_rejection(buyback, rejected)
    except Exception:
        pass  # worst case the lead is simply open to every partner in range
    messages.info(request, _('Offer declined - your book has gone back out to other nearby partners.'))
    return redirect('partners:buyback_detail', pk=pk)


# --- Pickup scheduling ----------------------------------------------------------

def _pickup_party(user, pk):
    """The accepted buyback `user` may schedule - its seller or its assigned,
    approved partner. 404 for anyone else (no leaking that it exists)."""
    buyback = get_object_or_404(BuybackRequest.objects.select_related('seller', 'partner__user'), pk=pk, status='accepted')
    partner = buyback.partner
    if user == buyback.seller or (partner and partner.user_id == user.pk and partner.is_approved):
        return buyback
    raise Http404


def _back_to_buyback(user, buyback):
    if user == buyback.seller:
        return redirect('partners:buyback_detail', pk=buyback.pk)
    return redirect('partners:lead_detail', pk=buyback.pk)


@require_POST
@login_required
def propose_pickup(request, pk):
    from bookmarket.notify import notify_pickup_proposed
    buyback = _pickup_party(request.user, pk)
    form = PickupScheduleForm(request.POST)
    if form.is_valid():
        buyback.pickup_date = form.cleaned_data['pickup_date']
        buyback.pickup_slot = form.cleaned_data['pickup_slot']
        buyback.pickup_proposed_by = request.user
        buyback.pickup_confirmed = False
        buyback.save(update_fields=['pickup_date', 'pickup_slot', 'pickup_proposed_by', 'pickup_confirmed', 'updated_at'])
        notify_pickup_proposed(buyback, request.user)
        messages.success(request, _('Proposed %(pickup_summary)s. Waiting for the other side to confirm.') % {'pickup_summary': buyback.pickup_summary})
    else:
        messages.error(request, ' '.join(e for errs in form.errors.values() for e in errs) or _('Invalid pickup time.'))
    return _back_to_buyback(request.user, buyback)


@require_POST
@login_required
def confirm_pickup(request, pk):
    from bookmarket.notify import notify_pickup_confirmed
    buyback = _pickup_party(request.user, pk)
    if not buyback.has_pickup_schedule or buyback.pickup_confirmed or buyback.pickup_proposed_by_id == request.user.pk:
        messages.error(request, _('There is no pickup time waiting for your confirmation.'))
        return _back_to_buyback(request.user, buyback)
    buyback.pickup_confirmed = True
    buyback.save(update_fields=['pickup_confirmed', 'updated_at'])
    if not buyback.pickup_otp:  # accepted before OTPs existed
        buyback.issue_pickup_otp()
    notify_pickup_confirmed(buyback, request.user)
    messages.success(request, _('Pickup confirmed for %(pickup_summary)s.') % {'pickup_summary': buyback.pickup_summary})
    return _back_to_buyback(request.user, buyback)


@require_POST
@login_required
def reissue_pickup_otp(request, pk):
    """Seller-only: new OTP (e.g. after a lock-out or if it was shared by mistake)."""
    from bookmarket.notify import notify_pickup_otp
    buyback = get_object_or_404(BuybackRequest, pk=pk, seller=request.user, status='accepted')
    buyback.issue_pickup_otp()
    notify_pickup_otp(buyback)
    messages.success(request, _('A new pickup OTP has been generated. The old one no longer works.'))
    return redirect('partners:buyback_detail', pk=pk)


@require_POST
@login_required
def cancel_request(request, pk):
    buyback = get_object_or_404(BuybackRequest, pk=pk, seller=request.user, status__in=['pending', 'offer_made'])
    buyback.status = 'cancelled'
    buyback.save(update_fields=['status', 'updated_at'])
    messages.info(request, _('Buyback request cancelled.'))
    return redirect('partners:buyback_list')


# --- Partner business tools ---------------------------------------------------

@login_required
def buyback_image(request, pk):
    """Read one buyback photo. Allowed: the seller who uploaded it, the approved partner who
    can see that lead (same rule as lead_detail), and staff who passed 2FA. Everyone else: 404."""
    from bookmarket.private_files import private_image_response
    image = get_object_or_404(BuybackRequestImage.objects.select_related('buyback_request'), pk=pk)
    lead = image.buyback_request
    allowed = lead.seller_id == request.user.pk or twofactor.staff_ok(request, 'cap_partners')
    if not allowed:
        partner = ShopkeeperPartner.objects.filter(user=request.user, status='approved').first()
        allowed = bool(partner) and (
            lead.partner_id == partner.pk
            or (lead.status == 'pending' and (lead.assigned_partner_id is None or lead.assigned_partner_id == partner.pk))
        )
    if not allowed:
        raise Http404
    return private_image_response(image.image)


@login_required
def lead_detail(request, pk):
    """Full view of one buyback lead for a partner: every detail, all photos,
    a map link, the Offer Assistant and the offer form."""
    partner, early = _partner_gate(request)
    if early:
        return early
    lead = get_object_or_404(
        BuybackRequest.objects.exclude(seller=request.user).filter(
            (Q(status='pending') & (Q(assigned_partner__isnull=True) | Q(assigned_partner=partner))) | Q(partner=partner)
        ),
        pk=pk,
    )
    reserved_for_me = lead.status == 'pending' and lead.assigned_partner_id == partner.pk
    maps_url = ''
    if lead.pickup_latitude is not None and lead.pickup_longitude is not None:
        maps_url = f'https://www.google.com/maps?q={lead.pickup_latitude},{lead.pickup_longitude}'
    return render(request, 'partners/lead_detail.html', {
        'partner': partner, 'lead': lead, 'maps_url': maps_url, 'offer_form': PartnerOfferForm(),
        'pickup_form': PickupScheduleForm(),
        'reserved_for_me': reserved_for_me, 'time_left': assignment.time_left_label(lead) if reserved_for_me else '',
    })


@require_POST
@login_required
def offer_suggestion(request, pk):
    """Offer Assistant: works backwards from the expected resale price to the
    most the partner can pay and still hit their target margin.

        profit = resale - offer * (1 + commission%)      (commission is charged on the price paid)
        offer <= resale * (1 - margin%) / (1 + commission%)
    """
    partner = _require_approved_partner(request.user)
    if not partner:
        return JsonResponse({'error': 'Only approved partners can use the Offer Assistant.'}, status=403)
    lead = get_object_or_404(
        BuybackRequest.objects.exclude(seller=request.user).filter(
            (Q(status='pending') & (Q(assigned_partner__isnull=True) | Q(assigned_partner=partner))) | Q(partner=partner)
        ),
        pk=pk,
    )

    result = recommend_price(
        title=lead.title, author=lead.author, isbn=lead.isbn, edition=lead.edition,
        condition=lead.condition, publication_year=lead.publication_year,
        original_price=lead.original_price, category_id=lead.category_id, subject=lead.subject,
    )
    resale = (Decimal(result['recommended_low']) + Decimal(result['recommended_high'])) / 2
    commission = partner.commission_percent / Decimal('100')
    margin = partner.target_margin_percent / Decimal('100')
    max_offer = resale * (1 - margin) / (1 + commission)
    max_offer = max(Decimal('1'), (max_offer / 5).quantize(Decimal('1')) * 5)  # round to the nearest \u20b95
    profit = resale - max_offer * (1 + commission)

    note = ''
    if lead.expected_price is not None:
        if lead.expected_price <= max_offer:
            note = f'The seller\u2019s ask (\u20b9{lead.expected_price}) fits your margin - you could offer that and still profit.'
        else:
            note = f'The seller is asking \u20b9{lead.expected_price}, above your margin-safe price - expect to negotiate.'
    return JsonResponse({
        'market_price': str(result['market_price']),
        'expected_resale': str(resale.quantize(Decimal('1'))),
        'suggested_offer': str(max_offer),
        'expected_profit': str(profit.quantize(Decimal('1'))),
        'margin_percent': str(partner.target_margin_percent),
        'note': note,
        'factors': result['factors'],
    })


def _month_key(dt):
    return dt.strftime('%Y-%m')


@login_required
def analytics(request):
    partner, early = _partner_gate(request)
    if early:
        return early

    completed = list(
        BuybackRequest.objects.filter(partner=partner, status='paid')
        .select_related('category', 'seller', 'listed_book', 'commission').order_by('completed_at')
    )
    listed_ids = [b.listed_book_id for b in completed if b.listed_book_id]
    sale_rows = list(
        Order.objects.filter(book_id__in=listed_ids, status='delivered').order_by('created_at')
        .values('book_id', 'final_price', 'created_at')
    )
    revenue_by_book, first_sale = defaultdict(Decimal), {}
    for row in sale_rows:
        revenue_by_book[row['book_id']] += row['final_price']
        first_sale.setdefault(row['book_id'], row['created_at'])

    def commission_of(b):
        return b.commission.amount if hasattr(b, 'commission') else Decimal('0')

    total_spend = sum((b.final_price or 0) for b in completed)
    total_commission = sum(commission_of(b) for b in completed)
    total_revenue = sum(revenue_by_book.values())

    # Realised profit only counts books that have actually sold, so unsold stock
    # doesn't show up as a loss.
    realised_profit, category_rows = Decimal('0'), defaultdict(lambda: {'bought': 0, 'spend': Decimal('0'), 'revenue': Decimal('0'), 'profit': Decimal('0')})
    days_to_sell, pay_ratios = [], []
    for b in completed:
        cost = (b.final_price or 0) + commission_of(b)
        rev = revenue_by_book.get(b.listed_book_id, Decimal('0'))
        row = category_rows[b.category.name if b.category else 'Uncategorised']
        row['bought'] += 1
        row['spend'] += b.final_price or 0
        if rev:
            row['revenue'] += rev
            row['profit'] += rev - cost
            realised_profit += rev - cost
            days_to_sell.append((first_sale[b.listed_book_id] - b.listed_book.created_at).days)
        if b.expected_price and b.final_price:
            pay_ratios.append(float(b.final_price / b.expected_price))

    relisted = [b for b in completed if b.listed_book_id]
    sold = [b for b in relisted if revenue_by_book.get(b.listed_book_id)]
    in_stock = [b for b in relisted if b.listed_book.status in ('available', 'reserved')]

    # Monthly series (last 12 months that have any activity)
    spend_m, revenue_m, count_m = defaultdict(float), defaultdict(float), defaultdict(int)
    for b in completed:
        k = _month_key(b.completed_at)
        spend_m[k] += float(b.final_price or 0)
        count_m[k] += 1
    for row in sale_rows:
        revenue_m[_month_key(row['created_at'])] += float(row['final_price'])
    months = sorted(set(spend_m) | set(revenue_m))[-12:]

    sellers = defaultdict(lambda: {'count': 0, 'paid': Decimal('0')})
    for b in completed:
        sellers[b.seller.username]['count'] += 1
        sellers[b.seller.username]['paid'] += b.final_price or 0
    top_sellers = sorted(sellers.items(), key=lambda kv: (-kv[1]['count'], -kv[1]['paid']))[:5]

    categories = sorted(category_rows.items(), key=lambda kv: (-kv[1]['profit'], -kv[1]['bought']))
    chart = {
        'labels': months,
        'spend': [round(spend_m[m], 2) for m in months],
        'revenue': [round(revenue_m[m], 2) for m in months],
        'count': [count_m[m] for m in months],
        'cat_labels': [name for name, _ in categories[:8]],
        'cat_profit': [float(r['profit']) for _, r in categories[:8]],
    }
    context = {
        'partner': partner,
        'has_data': bool(completed),
        'total_bought': len(completed),
        'total_spend': total_spend,
        'total_revenue': total_revenue,
        'total_commission': total_commission,
        'realised_profit': realised_profit,
        'margin_percent': round(float(realised_profit / total_revenue * 100), 1) if total_revenue else None,
        'relisted_count': len(relisted),
        'sold_count': len(sold),
        'sell_through': round(len(sold) / len(relisted) * 100) if relisted else None,
        'avg_days_to_sell': round(sum(days_to_sell) / len(days_to_sell), 1) if days_to_sell else None,
        'avg_pay_ratio': round(sum(pay_ratios) / len(pay_ratios) * 100) if pay_ratios else None,
        'stock_at_cost': sum((b.final_price or 0) for b in in_stock),
        'stock_at_list': sum(b.listed_book.price for b in in_stock),
        'stock_units': len(in_stock),
        'categories': categories,
        'top_sellers': top_sellers,
        'chart_json': json.dumps(chart),
        'month_count': len(months),
    }
    return render(request, 'partners/analytics.html', context)


@login_required
def inventory(request):
    """Every book the partner currently has on BookBridge, with the numbers
    (views, wishlists, offers, days on shelf, margin) needed to decide what to
    reprice."""
    partner, early = _partner_gate(request)
    if early:
        return early
    now = timezone.now()
    books = list(
        Book.objects.filter(seller=request.user).exclude(status='removed')
        .select_related('buyback_origin')
        .annotate(offer_count=Count('offers', distinct=True), wishlist_count=Count('wishlisted_by', distinct=True))
        .order_by('-created_at')
    )
    rows, tally = [], {'available': 0, 'stale': 0, 'sold': 0}
    for book in books:
        days = (now - book.created_at).days
        origin = getattr(book, 'buyback_origin', None)
        cost = profit = None
        if origin and origin.final_price is not None:
            cost = origin.final_price + (origin.commission.amount if hasattr(origin, 'commission') else Decimal('0'))
            profit = book.price - cost
        hint = ''
        if book.status == 'available':
            tally['available'] += 1
            if days >= 30 and book.views >= 15 and book.offer_count == 0:
                hint = 'Getting views but no offers - try lowering the price.'
            elif days >= 45:
                hint = 'Sitting for 45+ days - consider a price drop.'
            elif profit is not None and profit < 0:
                hint = 'Priced below what you paid - raise it or accept a loss knowingly.'
            if hint:
                tally['stale'] += 1
        elif book.status == 'sold':
            tally['sold'] += 1
        rows.append({'book': book, 'days': days, 'cost': cost, 'profit': profit, 'hint': hint})

    status = request.GET.get('status', '')
    if status == 'stale':
        rows = [r for r in rows if r['hint']]
    elif status in ('available', 'reserved', 'sold', 'under_review'):
        rows = [r for r in rows if r['book'].status == status]
    return render(request, 'partners/inventory.html', {
        'partner': partner, 'rows': rows, 'tally': tally, 'status': status,
    })


@login_required
def earnings(request):
    partner, early = _partner_gate(request)
    if early:
        return early
    commissions = partner.commissions.select_related('buyback_request').order_by('-created_at')
    due = partner.outstanding_commission()
    settled_total = sum(c.amount for c in commissions if c.is_settled)
    paid_out = BuybackRequest.objects.filter(partner=partner, status='paid')
    return render(request, 'partners/earnings.html', {
        'partner': partner, 'commissions': commissions, 'total_due': due, 'settled_total': settled_total,
        'buyback_count': paid_out.count(),
        'buyback_spend': sum((b.final_price or 0) for b in paid_out),
        'period_choices': period_choices(),
    })


@login_required
def export_csv(request):
    """CSV of every completed buyback - for the partner's own books / GST filing."""
    partner, early = _partner_gate(request)
    if early:
        return early
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="bookbridge-partner-buybacks.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'Completed on', 'Book', 'Author', 'ISBN', 'Seller', 'Paid to seller (INR)', 'Commission (INR)',
        'Commission settled', 'Resale price (INR)', 'Resale status',
    ])
    qs = BuybackRequest.objects.filter(partner=partner, status='paid').select_related(
        'seller', 'listed_book', 'commission',
    ).order_by('-completed_at')
    for b in qs:
        comm = b.commission if hasattr(b, 'commission') else None
        writer.writerow([
            b.completed_at.strftime('%Y-%m-%d') if b.completed_at else '', b.title, b.author, b.isbn,
            b.seller.username, b.final_price, comm.amount if comm else '',
            ('yes' if comm.is_settled else 'no') if comm else '',
            b.listed_book.price if b.listed_book else '', b.listed_book.get_status_display() if b.listed_book else 'Not relisted',
        ])
    return response


@require_POST
@login_required
def settle_all(request):
    partner, early = _partner_gate(request)
    if early:
        return early
    with transaction.atomic():
        pending = list(partner.commissions.select_for_update().filter(is_settled=False))
        total = sum((c.amount for c in pending), Decimal('0'))
        if not pending:
            messages.info(request, _('Nothing to settle.'))
            return redirect('partners:earnings')
        try:
            if total > 0:
                wallet_debit(
                    request.user, total, 'buyback_commission',
                    description=f'Commission for {len(pending)} buyback{"s" if len(pending) != 1 else ""}',
                )
        except InsufficientBalance:
            messages.error(request, _('Your wallet balance is too low to cover ₹%(total)s. Add funds to your wallet and try again.') % {'total': total})
            return redirect('partners:earnings')
        now = timezone.now()
        for c in pending:
            c.is_settled, c.settled_at = True, now
            c.save(update_fields=['is_settled', 'settled_at'])
    messages.success(request, ngettext('₹%(total)s commission settled across %(count)d buyback.', '₹%(total)s commission settled across %(count)d buybacks.', len(pending)) % {'total': total, 'count': len(pending)})
    return redirect('partners:earnings')


@login_required
def shop_settings(request):
    partner, early = _partner_gate(request)
    if early:
        return early
    if request.method == 'POST':
        form = PartnerSettingsForm(request.POST, instance=partner)
        if form.is_valid():
            form.save()
            messages.success(request, _('Shop settings saved.'))
            return redirect('partners:settings')
    else:
        form = PartnerSettingsForm(instance=partner)
    return render(request, 'partners/settings.html', {'partner': partner, 'form': form})
