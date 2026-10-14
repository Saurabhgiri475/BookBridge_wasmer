import mimetypes
from decimal import Decimal

from django.contrib import messages
from django.utils.translation import gettext as _
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from adminpanel.models import AuditLog
from accounts import twofactor
from adminpanel.views import staff_required
from bookmarket.security import rate_limit
from orders.models import Order

from . import services
from .forms import DisputeOpenForm, EscalateForm, MessageForm, StaffResolveForm
from .models import Dispute, DisputeEvidence

PAGE_SIZE = 20


def _participant_or_404(request, pk):
    dispute = get_object_or_404(Dispute.objects.select_related('order', 'buyer', 'seller'), pk=pk)
    if request.user.pk not in (dispute.buyer_id, dispute.seller_id):
        raise Http404  # a 404, not a 403: don't confirm the dispute exists
    return dispute


def _visible_thread(dispute):
    """Messages the buyer/seller may see, each with its photos."""
    return list(dispute.messages.filter(internal=False).select_related('author').prefetch_related('evidence'))


# --- Buyer ----------------------------------------------------------------------

@login_required
@rate_limit('dispute-open', 5, 3600)
def open_dispute(request, order_id):
    order = get_object_or_404(Order.objects.select_related('book__seller'), order_id=order_id, buyer=request.user)
    reason = services.blocked_reason(order)
    if reason:
        messages.info(request, reason)
        existing = order.disputes.exclude(status='withdrawn').first()
        if existing:
            return redirect('disputes:detail', pk=existing.pk)
        return redirect('orders:detail', order_id=order.order_id)

    if request.method == 'POST':
        form = DisputeOpenForm(request.POST, request.FILES, order=order)
        if form.is_valid():
            data = form.cleaned_data
            try:
                dispute = services.open_dispute(
                    order, request.user, category=data['category'], description=data['description'],
                    desired_resolution=data['desired_resolution'], requested_amount=data['requested_amount'],
                    files=data['evidence'],
                )
            except services.DisputeError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, _('Dispute %(reference)s opened. The seller has been notified.') % {'reference': dispute.reference})
                return redirect('disputes:detail', pk=dispute.pk)
    else:
        form = DisputeOpenForm(order=order)
    return render(request, 'disputes/open.html', {
        'form': form, 'order': order, 'response_days': services.SELLER_RESPONSE_DAYS,
    })


@login_required
def my_disputes(request):
    bought = Dispute.objects.filter(buyer=request.user).select_related('order')
    against = Dispute.objects.filter(seller=request.user).select_related('order')
    return render(request, 'disputes/my_disputes.html', {
        'bought': Paginator(bought, PAGE_SIZE).get_page(request.GET.get('bpage')),
        'against': Paginator(against, PAGE_SIZE).get_page(request.GET.get('spage')),
    })


@login_required
def dispute_detail(request, pk):
    if twofactor.staff_ok(request, 'cap_disputes'):
        dispute = get_object_or_404(Dispute, pk=pk)
        if request.user.pk not in (dispute.buyer_id, dispute.seller_id):
            return redirect('disputes:staff_detail', pk=pk)
    dispute = _participant_or_404(request, pk)
    is_buyer = request.user.pk == dispute.buyer_id
    return render(request, 'disputes/detail.html', {
        'dispute': dispute, 'thread': _visible_thread(dispute),
        'is_buyer': is_buyer, 'is_seller': not is_buyer,
        'message_form': MessageForm(), 'escalate_form': EscalateForm(),
        'can_reply': dispute.is_active,
    })


def _back(pk):
    return redirect('disputes:detail', pk=pk)


@login_required
@require_POST
@rate_limit('dispute-message', 20, 3600)
def post_message(request, pk):
    dispute = _participant_or_404(request, pk)
    form = MessageForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return _back(dispute.pk)
    try:
        services.post_message(dispute.pk, request.user, form.cleaned_data['body'], form.cleaned_data['evidence'])
    except services.DisputeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _('Reply sent.'))
    return _back(dispute.pk)


@login_required
@require_POST
def seller_agree(request, pk):
    dispute = _participant_or_404(request, pk)
    try:
        services.seller_agree(dispute.pk, request.user, request.POST.get('body', ''))
    except services.DisputeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _('Thanks - the BookBridge team will issue the refund.'))
    return _back(dispute.pk)


@login_required
@require_POST
def escalate(request, pk):
    dispute = _participant_or_404(request, pk)
    form = EscalateForm(request.POST)
    try:
        services.escalate(dispute.pk, request.user, form.cleaned_data.get('reason', '') if form.is_valid() else '')
    except services.DisputeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _('Escalated. The BookBridge team will review it.'))
    return _back(dispute.pk)


@login_required
@require_POST
def withdraw(request, pk):
    dispute = _participant_or_404(request, pk)
    try:
        services.withdraw(dispute.pk, request.user)
    except services.DisputeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _('Dispute withdrawn.'))
    return _back(dispute.pk)


@login_required
def evidence_file(request, pk):
    """The only way to read a dispute photo: the buyer, the seller or staff.
    Anyone else gets a 404 so the URL doesn't confirm a photo exists."""
    ev = get_object_or_404(DisputeEvidence.objects.select_related('dispute', 'message'), pk=pk)
    d = ev.dispute
    if request.user.pk not in (d.buyer_id, d.seller_id) and not twofactor.staff_ok(request, 'cap_disputes'):
        raise Http404
    if ev.message_id and ev.message.internal and request.user.pk in (d.buyer_id, d.seller_id):
        raise Http404
    try:
        handle = ev.file.open('rb')
    except (FileNotFoundError, ValueError):
        raise Http404
    ctype = mimetypes.guess_type(ev.file.name)[0] or 'application/octet-stream'
    response = FileResponse(handle, content_type=ctype)
    response['Content-Disposition'] = 'inline'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, max-age=0'
    return response


# --- Staff ------------------------------------------------------------------------

def _staff_filtered(request):
    qs = Dispute.objects.select_related('order', 'buyer', 'seller')
    status = request.GET.get('status', 'escalated')
    if status in dict(Dispute.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif status == 'active':
        qs = qs.filter(status__in=('open', 'seller_responded', 'escalated'))
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(order_ref__icontains=q) | Q(book_title__icontains=q) | Q(buyer__username__icontains=q)
            | Q(seller__username__icontains=q) | (Q(pk=int(q.upper().replace('DSP-', ''))) if q.upper().replace('DSP-', '').isdigit() else Q(pk__in=[]))
        )
    return qs, status, q


@staff_required('cap_disputes')
def staff_queue(request):
    qs, status, q = _staff_filtered(request)
    counts = Dispute.objects.aggregate(
        escalated=Count('id', filter=Q(status='escalated')),
        waiting=Count('id', filter=Q(status='open')),
        replied=Count('id', filter=Q(status='seller_responded')),
        resolved=Count('id', filter=Q(status='resolved')),
    )
    return render(request, 'disputes/staff_queue.html', {
        'page_obj': Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page')),
        'status': status, 'q': q, 'status_choices': Dispute.STATUS_CHOICES, 'counts': counts,
    })


@staff_required('cap_disputes')
def staff_detail(request, pk):
    dispute = get_object_or_404(Dispute.objects.select_related('order', 'buyer', 'seller', 'resolved_by'), pk=pk)
    order = dispute.order
    payment = getattr(order, 'payment', None) if order else None
    payout = getattr(order, 'payout', None) if order else None
    return_request = getattr(order, 'return_request', None) if order else None
    max_refund = services.refundable_amount(order) if order else Decimal('0.00')
    initial = {'outcome': 'refund', 'refund_amount': dispute.requested_amount or max_refund}
    return render(request, 'disputes/staff_detail.html', {
        'dispute': dispute, 'order': order, 'payment': payment, 'payout': payout, 'return_request': return_request,
        'thread': list(dispute.messages.select_related('author').prefetch_related('evidence')),
        'message_form': MessageForm(), 'resolve_form': StaffResolveForm(initial=initial),
        'max_refund': max_refund,
        'can_pay_online': bool(payment and payment.razorpay_payment_id),
        'buyer_paid': bool(payment and payment.status in services.PAID_PAYMENT_STATUSES),
    })


@require_POST
@staff_required('cap_disputes')
def staff_message(request, pk):
    form = MessageForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect('disputes:staff_detail', pk=pk)
    try:
        services.post_message(pk, request.user, form.cleaned_data['body'], form.cleaned_data['evidence'])
    except services.DisputeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _('Message sent to the buyer and seller.'))
    return redirect('disputes:staff_detail', pk=pk)


@require_POST
@staff_required('cap_disputes')
def staff_note(request, pk):
    try:
        services.staff_note(pk, request.user, request.POST.get('body', ''))
    except services.DisputeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _('Internal note saved (only staff can see it).'))
    return redirect('disputes:staff_detail', pk=pk)


@require_POST
@staff_required('cap_disputes')
def staff_resolve(request, pk):
    form = StaffResolveForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect('disputes:staff_detail', pk=pk)
    data = form.cleaned_data
    try:
        prior_status = Dispute.objects.filter(pk=pk).values_list('status', flat=True).first()
        dispute = services.staff_resolve(
            pk, request.user, outcome=data['outcome'], note=data['note'], refund_amount=data['refund_amount'],
            destination=data['destination'], deduct_from_seller=data['deduct_from_seller'],
        )
    except services.DisputeError as exc:
        messages.error(request, str(exc))
        return redirect('disputes:staff_detail', pk=pk)
    except Dispute.DoesNotExist:
        raise Http404
    summary = dispute.get_outcome_display() + (f' (Rs. {dispute.refund_amount})' if dispute.refund_amount else '')
    AuditLog.record(
        actor=request.user, action='resolve_dispute',
        target_repr=f'{dispute.reference} - {dispute.book_title}', target_id=dispute.pk,
        note=f'{summary}; {dispute.refund_destination or "no refund"}; ref {dispute.refund_reference or "-"}',
        request=request, old={'status': prior_status},
        new={'status': dispute.status, 'outcome': dispute.outcome, 'refund_amount': dispute.refund_amount,
             'destination': dispute.refund_destination},
    )
    messages.success(request, _('%(reference)s resolved: %(summary)s.') % {'reference': dispute.reference, 'summary': summary})
    return redirect('disputes:staff_queue')
