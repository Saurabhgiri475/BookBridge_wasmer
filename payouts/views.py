import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from adminpanel.models import AuditLog
from adminpanel.views import staff_required
from bookmarket.notify import notify_payouts_paid, notify_upi_changed
from bookmarket.security import rate_limit

from . import services, statements
from .forms import UpiForm
from .models import Payout, PayoutProfile
from .upi import upi_deeplink

PAGE_SIZE = 20
RECENT_UPI_CHANGE = timedelta(hours=72)


def _period_or_404(request):
    try:
        return statements.parse_period(request.GET.get('period'))
    except statements.InvalidPeriod:
        raise Http404('Unknown statement period.')


def _attachment(content, content_type, filename):
    response = HttpResponse(content, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# --- Seller -----------------------------------------------------------------

@login_required
def my_payouts(request):
    profile = PayoutProfile.objects.filter(user=request.user).first()
    qs = Payout.objects.filter(seller=request.user)
    status = request.GET.get('status', '')
    if status in dict(Payout.STATUS_CHOICES):
        qs = qs.filter(status=status)
    return render(request, 'payouts/my_payouts.html', {
        'upi_form': UpiForm(initial={'upi_id': profile.upi_id if profile else ''}),
        'upi_id': profile.upi_id if profile else '',
        'upi_name_verified': bool(profile and profile.is_name_verified),
        'account_name': request.user.get_full_name(),
        'totals': services.seller_totals(request.user),
        'page_obj': Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page')),
        'status': status,
        'status_choices': Payout.STATUS_CHOICES,
        'period_choices': statements.period_choices(),
    })


@login_required
@require_POST
@rate_limit('upi-change', 5, 3600)  # also caps password guessing through this form
def update_upi(request):
    form = UpiForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors['upi_id'][0])
        return redirect('payouts:mine')
    new_upi = form.cleaned_data['upi_id']
    previous = services.get_upi_id(request.user)
    if previous == new_upi:
        messages.success(request, _('Payout UPI ID saved.'))
        return redirect('payouts:mine')
    # Re-authenticate before changing where the money goes.
    if request.user.has_usable_password() and not request.user.check_password(form.cleaned_data.get('current_password') or ''):
        messages.error(request, _('Enter your account password to change your payout UPI ID.'))
        return redirect('payouts:mine')
    services.set_upi_id(request.user, new_upi, start_hold=True)
    # Changing where money goes is what an account takeover would do first,
    # so the real owner is always told, and it is written to the audit log.
    notify_upi_changed(request.user, previous, new_upi)
    from .upi import mask_upi_id
    AuditLog.record(
        actor=request.user, action='seller_change_upi', target_repr=request.user.username, target_id=request.user.pk,
        request=request, note=f'UPI changed from {mask_upi_id(previous) or "none"} to {mask_upi_id(new_upi)}; payout hold started.',
    )
    messages.success(
        request,
        _('Payout UPI ID saved. For your security, payouts to a new UPI ID start after %(hours)s hours.') % {'hours': int(services.UPI_HOLD.total_seconds() // 3600)},
    )
    return redirect('payouts:mine')


@login_required
def seller_statement_pdf(request):
    period = _period_or_404(request)
    payouts = statements.seller_payouts(request.user, period)
    pdf = statements.render_seller_statement_pdf(request.user, period, payouts)
    number = statements.statement_number('BBCS', request.user.pk, period)
    return _attachment(pdf, 'application/pdf', f'{number}.pdf')


@login_required
def seller_statement_csv(request):
    period = _period_or_404(request)
    payouts = statements.seller_payouts(request.user, period)
    number = statements.statement_number('BBCS', request.user.pk, period)
    return _attachment(
        statements.seller_statement_csv(request.user, period, payouts), 'text/csv; charset=utf-8', f'{number}.csv',
    )


# --- Partner (commission owed to BookBridge) ------------------------------

def _approved_partner_or_redirect(request):
    try:
        partner = request.user.partner_profile
    except ObjectDoesNotExist:
        partner = None
    if not partner or not partner.is_approved:
        messages.info(request, _('You need to be an approved Shopkeeper Partner to download this.'))
        return None
    return partner


@login_required
def partner_statement_pdf(request):
    partner = _approved_partner_or_redirect(request)
    if not partner:
        return redirect('partners:apply')
    period = _period_or_404(request)
    rows = statements.partner_commissions(partner, period)
    number = statements.statement_number('BBPS', partner.pk, period)
    return _attachment(statements.render_partner_statement_pdf(partner, period, rows), 'application/pdf', f'{number}.pdf')


@login_required
def partner_statement_csv(request):
    partner = _approved_partner_or_redirect(request)
    if not partner:
        return redirect('partners:apply')
    period = _period_or_404(request)
    rows = statements.partner_commissions(partner, period)
    number = statements.statement_number('BBPS', partner.pk, period)
    return _attachment(statements.partner_statement_csv(partner, period, rows), 'text/csv; charset=utf-8', f'{number}.csv')


# --- Staff ------------------------------------------------------------------

def _staff_filtered(request):
    qs = Payout.objects.select_related('seller', 'seller__payout_profile', 'order')
    status = request.GET.get('status', 'pending')
    if status in dict(Payout.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif status == 'recover':
        qs = qs.filter(refunded_after_payout=True)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(seller__username__icontains=q) | Q(utr__icontains=q) | Q(order_ref__icontains=q) | Q(book_title__icontains=q)
        )
    seller_id = request.GET.get('seller', '')
    if seller_id.isdigit():
        qs = qs.filter(seller_id=int(seller_id))
    return qs, status, q, seller_id


def _decorate(payout):
    profile = getattr(payout.seller, 'payout_profile', None)
    payout.current_upi = profile.upi_id if profile else ''
    left = services.upi_hold_remaining(profile)
    payout.upi_hold_left = services.format_hold(left) if left else ''
    payout.upi_hold_overridden = bool(
        profile and profile.hold_override_at and profile.hold_override_upi == profile.upi_id
        and profile.upi_changed_at and timezone.now() - profile.upi_changed_at < services.UPI_HOLD
    )
    payout.upi_recently_changed = bool(left)
    payout.name_check_needed = services.needs_name_check(payout.seller, payout.current_upi)
    payout.name_verified = bool(profile and profile.is_name_verified)
    payout.verified_name = profile.verified_name if payout.name_verified else ''
    payout.name_check_override = bool(profile and profile.is_name_verified and profile.name_check_override)
    payout.account_name = payout.seller.get_full_name()
    if payout.current_upi and payout.status == 'pending':
        payout.upi_link = upi_deeplink(
            payout.current_upi, payout.seller.get_full_name() or payout.seller.username,
            payout.net_amount, f'BookBridge payout #{payout.order_ref}',
        )
    return payout


@staff_required('cap_finance')
def staff_queue(request):
    qs, status, q, seller_id = _staff_filtered(request)
    page_obj = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    for p in page_obj:
        _decorate(p)

    totals = Payout.objects.aggregate(
        pending=Sum('net_amount', filter=Q(status='pending')), pending_n=Count('id', filter=Q(status='pending')),
        on_hold=Sum('net_amount', filter=Q(status='on_hold')),
        paid=Sum('net_amount', filter=Q(status='paid')),
        recover=Sum('net_amount', filter=Q(refunded_after_payout=True)),
        recover_n=Count('id', filter=Q(refunded_after_payout=True)),
    )
    by_seller = list(
        Payout.objects.filter(status='pending').values('seller_id', 'seller__username')
        .annotate(total=Sum('net_amount'), n=Count('id')).order_by('-total')[:15]
    )
    return render(request, 'payouts/staff_queue.html', {
        'page_obj': page_obj, 'status': status, 'q': q, 'seller_id': seller_id,
        'status_choices': Payout.STATUS_CHOICES, 'totals': totals, 'by_seller': by_seller,
    })


@staff_required('cap_finance')
def staff_payout_detail(request, pk):
    payout = _decorate(get_object_or_404(Payout.objects.select_related('seller', 'seller__payout_profile', 'order', 'paid_by'), pk=pk))
    return render(request, 'payouts/staff_detail.html', {'payout': payout})


def _audit(request, action, payouts, note=''):
    first = payouts[0]
    total = sum((p.net_amount for p in payouts), Decimal('0.00'))
    AuditLog.record(
        actor=request.user, action=action,
        target_repr=f'{len(payouts)} payout(s), Rs. {total} to {first.seller.username}',
        target_id=first.pk, note=note, request=request,
        new={'action': action, 'payout_ids': [p.pk for p in payouts][:50], 'count': len(payouts), 'total': total},
    )


@require_POST
@staff_required('cap_finance')
def staff_mark_paid(request):
    ids = [int(i) for i in request.POST.getlist('payout_ids') if i.isdigit()]
    utr = request.POST.get('utr', '')
    back = request.POST.get('next') or ''
    dest = redirect('payouts:staff_queue')
    if not ids:
        messages.error(request, _('Select at least one payout.'))
        return dest
    try:
        payouts = services.mark_paid(ids, utr=utr, actor=request.user)
    except services.PayoutError as exc:
        messages.error(request, str(exc))
        return redirect('payouts:staff_detail', pk=ids[0]) if back == 'detail' and len(ids) == 1 else dest
    _audit(request, 'mark_payout_paid', payouts, note=f'UTR {payouts[0].utr}; UPI {payouts[0].paid_to_upi}')
    notify_payouts_paid(payouts[0].seller, payouts)
    total = sum((p.net_amount for p in payouts), Decimal('0.00'))
    messages.success(request, _('Recorded Rs. %(total)s paid to %(username)s (UTR %(utr)s).') % {'total': total, 'username': payouts[0].seller.username, 'utr': payouts[0].utr})
    return dest


@require_POST
@staff_required('cap_finance')
def staff_payout_action(request, pk):
    action = request.POST.get('action')
    note = request.POST.get('note', '')
    try:
        if action == 'hold':
            payout = services.put_on_hold(pk, note=note)
            audit = 'hold_payout'
        elif action == 'unhold':
            payout = services.release_hold(pk, note=note)
            audit = 'release_payout_hold'
        elif action == 'cancel':
            payout = services.cancel(pk, note=note)
            audit = 'cancel_payout'
        else:
            raise services.PayoutError('Unknown action.')
    except Payout.DoesNotExist:
        raise Http404
    except services.PayoutError as exc:
        messages.error(request, str(exc))
        return redirect('payouts:staff_detail', pk=pk)
    _audit(request, audit, [payout], note=note.strip())
    messages.success(request, _('Payout is now %(status)s.') % {'status': payout.get_status_display().lower()})
    return redirect('payouts:staff_detail', pk=pk)


@require_POST
@staff_required('cap_finance')
def staff_override_upi_hold(request, pk):
    """Staff waive the post-UPI-change payout hold for this seller's current UPI ID. A reason is
    mandatory and the waiver is written to the audit log."""
    payout = get_object_or_404(Payout.objects.select_related('seller'), pk=pk)
    seller = payout.seller
    reason = request.POST.get('reason', '')
    try:
        profile = services.override_upi_hold(seller, actor=request.user, reason=reason)
    except services.PayoutError as exc:
        messages.error(request, str(exc))
        return redirect('payouts:staff_detail', pk=pk)
    AuditLog.record(
        actor=request.user, action='override_upi_hold', target_repr=f'{seller.username} - {profile.upi_id}',
        target_id=seller.pk, request=request, reason=reason.strip(),
        note='Payout hold after UPI change waived by staff.', new={'upi_id': profile.upi_id},
    )
    messages.warning(request, _('Hold overridden - recorded in the audit log.'))
    return redirect('payouts:staff_detail', pk=pk)


@require_POST
@staff_required('cap_finance')
def staff_check_upi_name(request, pk):
    """Staff record the account-holder name their UPI app showed for this seller's UPI ID."""
    payout = get_object_or_404(Payout.objects.select_related('seller'), pk=pk)
    seller = payout.seller
    upi_name = request.POST.get('upi_name', '')
    override = request.POST.get('override') == 'on'
    note = request.POST.get('note', '')
    upi_id = services.get_upi_id(seller)
    try:
        profile, matched = services.record_name_check(seller, upi_name, actor=request.user, override=override, note=note)
    except services.PayoutError as exc:
        messages.error(request, str(exc))
        return redirect('payouts:staff_detail', pk=pk)
    AuditLog.record(
        actor=request.user, action='check_upi_name', target_repr=f'{seller.username} - {upi_id}',
        target_id=seller.pk, request=request, reason=note.strip(),
        note=f'UPI app showed "{profile.verified_name}"; account name "{seller.get_full_name()}"; '
             f'{"matched" if matched else "ACCEPTED DESPITE MISMATCH"}',
        new={'upi_id': upi_id, 'verified_name': profile.verified_name, 'matched': matched},
    )
    if matched:
        messages.success(request, _('UPI name confirmed for %(username)s.') % {'username': seller.username})
    else:
        messages.warning(request, _('Accepted despite a name mismatch - recorded in the audit log.') % {})
    return redirect('payouts:staff_detail', pk=pk)


@staff_required('cap_finance')
def staff_export_csv(request):
    """The payout ledger as a spreadsheet, for reconciling against the bank/UPI
    app statement. Honours the same filters as the queue."""
    qs, _a, _b, _c = _staff_filtered(request)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="bookbridge-payouts.csv"'
    w = csv.writer(response)
    w.writerow([
        'Released', 'Order', 'Seller', 'Book', 'Sale price', 'Commission %', 'Commission', 'Net payout', 'Status',
        'Paid on', 'Paid to UPI', 'UTR', 'Recorded by', 'Refunded after payout', 'Note',
    ])
    for p in qs.select_related('paid_by').order_by('released_at', 'id').iterator():
        w.writerow([
            statements._ist_date(p.released_at), p.order_ref, statements.csv_safe(p.seller.username),
            statements.csv_safe(p.book_title), p.gross_amount, p.commission_percent, p.commission_amount, p.net_amount,
            p.get_status_display(), statements._ist_date(p.paid_at), statements.csv_safe(p.paid_to_upi),
            statements.csv_safe(p.utr), statements.csv_safe(p.paid_by.username if p.paid_by else ''),
            'yes' if p.refunded_after_payout else '', statements.csv_safe(p.note.replace('\n', ' | ')),
        ])
    return response
