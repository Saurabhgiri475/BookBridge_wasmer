"""
A single in-app Staff Dashboard - the operational surface for the things
staff actually need to act on day-to-day (listings Fraud Detection held
for review, accounts flagged as possible duplicates, returns waiting on a
decision), styled like the rest of BookBridge instead of Django admin's
generic changelist UI.

This deliberately does NOT try to replace Django admin (/admin/) - that's
still where you'd bulk-edit raw rows, manage every model, or run the
Razorpay-refund/wallet-refund actions on ReturnRequest (see
orders/admin.py:ReturnRequestAdmin) - refund logic that moves real money
is left there rather than duplicated here. This page is the "what needs my
attention right now" view; Django admin is still the "manage everything"
view.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.fraud_detection import find_linked_accounts
from accounts import twofactor
from accounts.models import User
from books.fraud_detection import WARN_THRESHOLD
from books.models import Book, ListingReport
from donations.models import Donation
from orders.models import Order, ReturnRequest
from disputes.models import Dispute
from partners.forms import PartnerRejectForm
from partners.models import ShopkeeperPartner

from bookmarket.notify import notify_listing_approved, notify_listing_removed, notify_partner_application_decision
from .models import AuditLog

# Each dashboard list paginates independently, so each gets its own page
# query param (?review_page=, ?flagged_page=, ?returns_page=, ?reports_page=,
# ?live_flagged_page=) rather than sharing "page" - otherwise paging one
# list would silently reset the others back to page 1. Search boxes follow
# the same per-list convention (?review_q=, ?live_flagged_q=, ?flagged_q=).
PAGE_SIZE = 15
# Recent staff activity - see AuditLog's docstring: "write-only ... nothing
# reads these back yet". This is that read: the last N actions taken from
# this dashboard, most recent first.
RECENT_ACTIVITY_LIMIT = 20


def staff_required(arg=None, *, capability=None):
    """login_required + is_staff + two-factor, bounced through the site's own login page
    (accounts:login) rather than Django admin's - a non-staff user hitting this just looks
    logged-out-and-redirected, not like a permissions error page.

    Use it bare (`@staff_required`) for pages any staff member may open, or with a capability
    (`@staff_required('cap_finance')`) to restrict a page to staff whose role includes it
    (adminpanel/roles.py). Staff without the capability get a 403 and the attempt is not silent
    for them; superusers always pass."""
    def decorate(view_func):
        def capability_gate(request, *args, **kwargs):
            if capability and not request.user.has_perm(f'adminpanel.{capability}'):
                raise PermissionDenied('Your staff role does not include this area.')
            return view_func(request, *args, **kwargs)
        capability_gate.__name__ = getattr(view_func, '__name__', 'staff_view')
        capability_gate.__doc__ = view_func.__doc__
        # Staff must also have passed two-factor authentication this session (accounts/twofactor.py).
        return login_required(user_passes_test(lambda u: u.is_staff)(twofactor.staff_2fa_gate(capability_gate)))

    if callable(arg):          # used as plain @staff_required
        return decorate(arg)
    capability = arg or capability
    return decorate


@staff_required
def dashboard(request):
    review_q = request.GET.get('review_q', '').strip()
    live_flagged_q = request.GET.get('live_flagged_q', '').strip()
    flagged_q = request.GET.get('flagged_q', '').strip()

    books_under_review_qs = Book.objects.filter(status='under_review').select_related('seller').order_by('-fraud_checked_at')
    if review_q:
        books_under_review_qs = books_under_review_qs.filter(
            Q(title__icontains=review_q) | Q(seller__username__icontains=review_q)
        )
    # Medium-risk listings (score >= WARN_THRESHOLD but below FLAG_THRESHOLD)
    # publish immediately rather than being held - see
    # books/fraud_detection.py's module docstring - but they still carry a
    # fraud_score/fraud_flags worth a staff look, so surface them here too
    # instead of leaving them visible only in raw Django admin.
    flagged_live_books_qs = Book.objects.filter(
        status='available', fraud_score__gte=WARN_THRESHOLD,
    ).select_related('seller').order_by('-fraud_score', '-fraud_checked_at')
    if live_flagged_q:
        flagged_live_books_qs = flagged_live_books_qs.filter(
            Q(title__icontains=live_flagged_q) | Q(seller__username__icontains=live_flagged_q)
        )
    flagged_users_qs = User.objects.filter(is_duplicate_flagged=True).order_by('-date_joined')
    if flagged_q:
        flagged_users_qs = flagged_users_qs.filter(username__icontains=flagged_q)
    pending_returns_qs = ReturnRequest.objects.filter(status='requested').select_related(
        'order__book', 'order__buyer',
    ).order_by('-created_at')
    pending_reports_qs = ListingReport.objects.filter(status='pending').select_related('book', 'reporter')
    pending_partners_qs = ShopkeeperPartner.objects.filter(status='pending').select_related('user').order_by('applied_at')

    # Stats always reflect the true unfiltered totals (search only narrows
    # the table below each badge, not what the badge itself counts) - so a
    # search that matches nothing doesn't make it look like the queue is
    # empty.
    stats = {
        'total_users': User.objects.count(),
        'books_listed': Book.objects.filter(status='available').count(),
        'total_orders': Order.objects.count(),
        'pending_review_count': Book.objects.filter(status='under_review').count(),
        'flagged_live_count': Book.objects.filter(status='available', fraud_score__gte=WARN_THRESHOLD).count(),
        'flagged_account_count': User.objects.filter(is_duplicate_flagged=True).count(),
        'pending_return_count': pending_returns_qs.count(),
        'escalated_dispute_count': Dispute.objects.filter(status='escalated').count(),
        'pending_report_count': pending_reports_qs.count(),
        'pending_partner_count': pending_partners_qs.count(),
        'pending_donation_count': Donation.objects.filter(status__in=('submitted', 'accepted')).count(),
    }

    books_under_review = Paginator(books_under_review_qs, PAGE_SIZE).get_page(request.GET.get('review_page'))
    flagged_live_books = Paginator(flagged_live_books_qs, PAGE_SIZE).get_page(request.GET.get('live_flagged_page'))
    flagged_users = Paginator(flagged_users_qs, PAGE_SIZE).get_page(request.GET.get('flagged_page'))
    # Only computed for the current page (bounded by PAGE_SIZE), not the
    # whole flagged_users_qs, to avoid re-running find_linked_accounts for
    # every flagged account on every dashboard load - it re-derives the
    # match live (rather than re-parsing duplicate_flag_reason's stored
    # text) so staff can jump straight to the account it's flagged
    # against instead of piecing that together from free text. Attached
    # directly on each object (rather than a separate pk-keyed dict) since
    # plain Django templates can't do dict[variable_key] lookups.
    for flagged_user in flagged_users:
        flagged_user.linked_matches = find_linked_accounts(flagged_user)
    pending_returns = Paginator(pending_returns_qs, PAGE_SIZE).get_page(request.GET.get('returns_page'))
    pending_reports = Paginator(pending_reports_qs, PAGE_SIZE).get_page(request.GET.get('reports_page'))
    pending_partners = Paginator(pending_partners_qs, PAGE_SIZE).get_page(request.GET.get('partners_page'))
    recent_activity = AuditLog.objects.select_related('actor').order_by('-created_at')[:RECENT_ACTIVITY_LIMIT]

    return render(request, 'adminpanel/dashboard.html', {
        'stats': stats,
        'books_under_review': books_under_review,
        'review_q': review_q,
        'flagged_live_books': flagged_live_books,
        'live_flagged_q': live_flagged_q,
        'flagged_users': flagged_users,
        'flagged_q': flagged_q,
        'pending_returns': pending_returns,
        'pending_reports': pending_reports,
        'pending_partners': pending_partners,
        'partner_reject_form': PartnerRejectForm(),
        'recent_activity': recent_activity,
    })


# --- Revenue dashboard ------------------------------------------------
# GMV/commission are computed the same way Order.commission_amount/
# seller_payout already define them per-order (see orders/models.py) - just
# aggregated across delivered orders instead of read off one Order at a
# time. Only 'delivered' orders count towards revenue, same convention the
# seller-facing accounts.views.dashboard stat already uses.
REVENUE_MONTHS_BACK = 12

_COMMISSION_EXPR = ExpressionWrapper(
    F('final_price') * F('platform_commission_percent') / 100,
    output_field=DecimalField(max_digits=10, decimal_places=2),
)
_GMV_EXPR = ExpressionWrapper(
    F('final_price') + F('delivery_charge') - F('coupon_discount'),
    output_field=DecimalField(max_digits=10, decimal_places=2),
)


@staff_required('cap_finance')
def revenue_dashboard(request):
    since = timezone.now() - timezone.timedelta(days=30 * REVENUE_MONTHS_BACK)
    delivered = Order.objects.filter(status='delivered')

    overview = delivered.aggregate(
        gmv=Sum(_GMV_EXPR), commission=Sum(_COMMISSION_EXPR), order_count=Count('id'),
    )

    monthly = list(
        delivered.filter(created_at__gte=since)
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(gmv=Sum(_GMV_EXPR), commission=Sum(_COMMISSION_EXPR), order_count=Count('id'))
        .order_by('month')
    )

    top_sellers = list(
        delivered.values('book__seller__id', 'book__seller__username')
        .annotate(revenue=Sum('final_price'), commission=Sum(_COMMISSION_EXPR), order_count=Count('id'))
        .order_by('-revenue')[:10]
    )

    chart_data = {
        'labels': [row['month'].strftime('%b %Y') for row in monthly],
        'gmv': [float(row['gmv'] or 0) for row in monthly],
        'commission': [float(row['commission'] or 0) for row in monthly],
        'order_count': [row['order_count'] for row in monthly],
    }

    order_count = overview['order_count'] or 0
    commission = overview['commission'] or 0
    avg_commission_per_order = round(commission / order_count, 0) if order_count else None

    # Partner buyback commissions (see partners/models.py:PartnerCommission)
    # are a separate revenue stream from marketplace order commission above -
    # shopkeeper partners owe BookBridge this cut on books they buy direct
    # from sellers, settled via their own wallet (partners/views.py:settle_commission).
    from partners.models import PartnerCommission
    partner_commission_totals = PartnerCommission.objects.aggregate(
        total_due=Sum('amount', filter=Q(is_settled=False)),
        total_settled=Sum('amount', filter=Q(is_settled=True)),
    )

    return render(request, 'adminpanel/revenue.html', {
        'gmv': overview['gmv'] or 0,
        'commission': commission,
        'order_count': order_count,
        'avg_commission_per_order': avg_commission_per_order,
        'top_sellers': top_sellers,
        'chart_data_json': json.dumps(chart_data),
        'partner_commission_due': partner_commission_totals['total_due'] or 0,
        'partner_commission_settled': partner_commission_totals['total_settled'] or 0,
    })


@require_POST
@staff_required('cap_moderation')
def approve_book(request, pk):
    """Publishes a listing Fraud Detection held for review - see
    books/fraud_detection.py and books/views.py:add_book/edit_book."""
    book = get_object_or_404(Book, pk=pk)
    old_status = book.status
    book.status = 'available'
    book.save(update_fields=['status'])
    notify_listing_approved(book)
    AuditLog.record(actor=request.user, action='approve_book', target_repr=book.title, target_id=book.pk,
                    request=request, old={'status': old_status}, new={'status': book.status})
    messages.success(request, _('"%(title)s" is now live.') % {'title': book.title})
    return redirect('adminpanel:dashboard')


@require_POST
@staff_required('cap_moderation')
def remove_book(request, pk):
    """Confirms the fraud flag was correct - takes the listing down for good."""
    book = get_object_or_404(Book, pk=pk)
    reason = request.POST.get('reason', '').strip()
    old_status = book.status
    book.status = 'removed'
    book.save(update_fields=['status'])
    book.reports.filter(status='pending').update(status='actioned')
    notify_listing_removed(book)
    AuditLog.record(actor=request.user, action='remove_book', target_repr=book.title, target_id=book.pk, note=reason,
                    request=request, old={'status': old_status}, new={'status': book.status}, reason=reason)
    messages.success(request, _('"%(title)s" has been removed.') % {'title': book.title})
    return redirect('adminpanel:dashboard')


@require_POST
@staff_required('cap_fraud')
def clear_fraud_flag(request, pk):
    """A live listing that Fraud Detection flagged at WARN_THRESHOLD (but
    not high enough to hold for review) turns out to be fine - clears the
    score/flags so it drops off this list, without touching its status
    (it's already 'available' and stays that way). To act on it instead,
    use the listing's own Remove button."""
    book = get_object_or_404(Book, pk=pk, status='available')
    reason = request.POST.get('reason', '').strip()
    flags_summary = ', '.join(f['check'] for f in book.get_fraud_flags()) or 'no flags recorded'
    note = f'Previous flags: {flags_summary}'
    if reason:
        note += f' | Staff note: {reason}'
    old = {'fraud_score': book.fraud_score, 'fraud_flags': flags_summary}
    book.fraud_score = 0
    book.fraud_flags = ''
    book.save(update_fields=['fraud_score', 'fraud_flags'])
    AuditLog.record(actor=request.user, action='clear_fraud_flag', target_repr=book.title, target_id=book.pk, note=note,
                    request=request, old=old, new={'fraud_score': 0, 'fraud_flags': ''}, reason=reason)
    messages.success(request, _('Fraud flag cleared for "%(title)s".') % {'title': book.title})
    return redirect('adminpanel:dashboard')


@require_POST
@staff_required('cap_fraud')
def clear_duplicate_flag(request, pk):
    """Confirms a duplicate-phone match flagged by
    accounts/fraud_detection.py was a false positive (e.g. family members
    sharing a phone number) rather than one seller running two accounts."""
    flagged_user = get_object_or_404(User, pk=pk)
    reason = request.POST.get('reason', '').strip()
    original_reason = flagged_user.duplicate_flag_reason
    note = original_reason
    if reason:
        note = f'{original_reason} | Staff note: {reason}' if original_reason else f'Staff note: {reason}'
    was_flagged = flagged_user.is_duplicate_flagged
    flagged_user.is_duplicate_flagged = False
    flagged_user.duplicate_flag_reason = ''
    flagged_user.save(update_fields=['is_duplicate_flagged', 'duplicate_flag_reason'])
    AuditLog.record(
        actor=request.user, action='clear_duplicate_flag', target_repr=flagged_user.username,
        target_id=flagged_user.pk, note=note, request=request, reason=reason,
        old={'is_duplicate_flagged': was_flagged, 'reason': original_reason}, new={'is_duplicate_flagged': False},
    )
    messages.success(request, _('Duplicate-account flag cleared for %(username)s.') % {'username': flagged_user.username})
    return redirect('adminpanel:dashboard')


@require_POST
@staff_required('cap_moderation')
def dismiss_report(request, pk):
    """A buyer-filed ListingReport turned out not to need action (e.g.
    duplicate report, or the listing already looks fine) - marks it
    reviewed without touching the listing itself. To act on a report
    instead, use the listing's own Approve/Remove buttons above (which
    also auto-marks any pending reports on that book as 'actioned')."""
    report = get_object_or_404(ListingReport, pk=pk, status='pending')
    report.status = 'dismissed'
    report.save(update_fields=['status'])
    AuditLog.record(
        actor=request.user, action='dismiss_report', target_repr=report.book.title,
        target_id=report.book_id, note=f'Report reason: {report.get_reason_display()}',
        request=request, old={'status': 'pending'}, new={'status': 'dismissed'},
    )
    messages.success(request, _('Report on "%(title)s" dismissed.') % {'title': report.book.title})
    return redirect('adminpanel:dashboard')


@require_POST
@staff_required('cap_partners')
def approve_partner(request, pk):
    """Approves a Shopkeeper Partner application - see partners/models.py
    for the buyback flow this unlocks for them."""
    partner = get_object_or_404(ShopkeeperPartner, pk=pk, status='pending')
    partner.status = 'approved'
    partner.reviewed_at = timezone.now()
    partner.reviewed_by = request.user
    partner.save(update_fields=['status', 'reviewed_at', 'reviewed_by'])
    AuditLog.record(
        actor=request.user, action='approve_partner', target_repr=partner.shop_name, target_id=partner.pk,
        request=request, old={'status': 'pending'}, new={'status': 'approved'},
    )
    notify_partner_application_decision(partner)
    messages.success(request, _('"%(shop_name)s" is now an approved BookBridge Partner.') % {'shop_name': partner.shop_name})
    return redirect('adminpanel:dashboard')


@require_POST
@staff_required('cap_partners')
def reject_partner(request, pk):
    partner = get_object_or_404(ShopkeeperPartner, pk=pk, status='pending')
    form = PartnerRejectForm(request.POST)
    reason = form.cleaned_data['reason'] if form.is_valid() else ''
    partner.status = 'rejected'
    partner.rejection_reason = reason
    partner.reviewed_at = timezone.now()
    partner.reviewed_by = request.user
    partner.save(update_fields=['status', 'rejection_reason', 'reviewed_at', 'reviewed_by'])
    AuditLog.record(
        actor=request.user, action='reject_partner', target_repr=partner.shop_name, target_id=partner.pk, note=reason,
        request=request, old={'status': 'pending'}, new={'status': 'rejected'}, reason=reason,
    )
    notify_partner_application_decision(partner)
    messages.success(request, _('Application from "%(shop_name)s" rejected.') % {'shop_name': partner.shop_name})
    return redirect('adminpanel:dashboard')
