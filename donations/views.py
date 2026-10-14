from django.contrib import messages
from django.utils.translation import gettext as _
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from adminpanel.models import AuditLog
from accounts import twofactor
from adminpanel.views import staff_required
from bookmarket.notify import (
    notify_certificate_issued, notify_certificate_revoked, notify_donation_accepted,
    notify_donation_rejected, notify_donation_submitted,
)
from bookmarket.security import rate_limit

from . import services
from .certificate import render_certificate_pdf
from .forms import DonationForm, DonationItemFormSet, ReasonForm, ReceiveForm
from .models import Donation, DonationCertificate, DonationPartner

PAGE_SIZE = 20


def _normalize_code(raw):
    return (raw or '').strip().upper().replace(' ', '')[:20]


def _own_or_staff(request, donation):
    if donation.donor_id != request.user.id and not twofactor.staff_ok(request, 'cap_donations'):
        raise Http404  # don't reveal that someone else's donation exists
    return donation


# --- Public ---------------------------------------------------------------------

def home(request):
    partners = DonationPartner.objects.filter(is_active=True)
    return render(request, 'donations/home.html', {
        'partners': partners,
        'stats': services.donation_stats(),
    })


@rate_limit('donation_verify', 30, 60, by='ip')
def verify(request, code=None):
    """Public certificate check. GET /donate/verify/ shows the box; the code can come
    from the URL (QR code), or ?code= (the box)."""
    raw = code or request.GET.get('code')
    if raw and not code:
        return redirect('donations:verify', code=_normalize_code(raw))
    certificate = None
    searched = bool(code)
    if code:
        certificate = DonationCertificate.objects.filter(code=_normalize_code(code)).select_related('donation').first()
    return render(request, 'donations/verify.html', {
        'certificate': certificate, 'searched': searched, 'code': _normalize_code(code) if code else '',
    })


# --- Donor ----------------------------------------------------------------------

@login_required
@rate_limit('donation_create', 10, 3600)
def create(request):
    if not DonationPartner.objects.filter(is_active=True).exists():
        messages.info(request, _('No organisations are accepting donations yet. Please check back soon.'))
        return redirect('donations:home')

    if request.method == 'POST':
        form = DonationForm(request.POST)
        formset = DonationItemFormSet(request.POST, instance=Donation())
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                donation = form.save(commit=False)
                donation.donor = request.user
                donation.save()
                formset.instance = donation
                formset.save()
            notify_donation_submitted(donation)
            messages.success(request, _('Thank you! Your donation request has been submitted.'))
            return redirect('donations:detail', pk=donation.pk)
    else:
        form = DonationForm(initial={
            'certificate_name': request.user.get_full_name() or request.user.username,
            'contact_phone': request.user.phone, 'pickup_address': request.user.address,
            'pickup_city': request.user.city, 'pickup_pincode': request.user.pincode,
            'partner': request.GET.get('partner') if request.GET.get('partner', '').isdigit() else None,
        })
        formset = DonationItemFormSet(instance=Donation())
    return render(request, 'donations/form.html', {'form': form, 'formset': formset})


@login_required
def my_donations(request):
    donations = Donation.objects.filter(donor=request.user).select_related('partner', 'certificate').prefetch_related('items')
    return render(request, 'donations/my_donations.html', {'donations': donations})


@login_required
def detail(request, pk):
    donation = _own_or_staff(request, get_object_or_404(
        Donation.objects.select_related('partner', 'donor'), pk=pk))
    return render(request, 'donations/detail.html', {
        'donation': donation, 'items': donation.items.all(),
        'certificate': getattr(donation, 'certificate', None),
    })


@login_required
@require_POST
def cancel(request, pk):
    donation = get_object_or_404(Donation, pk=pk, donor=request.user)
    try:
        services.cancel_donation(donation)
        messages.success(request, _('Donation cancelled.'))
    except services.DonationStateError as e:
        messages.error(request, str(e))
    return redirect('donations:detail', pk=pk)


@login_required
def certificate_pdf(request, code):
    cert = get_object_or_404(DonationCertificate.objects.select_related('donation'), code=_normalize_code(code))
    _own_or_staff(request, cert.donation)
    if cert.revoked:
        messages.error(request, _('This certificate has been revoked and can no longer be downloaded.'))
        return redirect('donations:detail', pk=cert.donation_id)
    response = HttpResponse(render_certificate_pdf(cert), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="donation-certificate-{cert.code}.pdf"'
    return response


# --- Staff ----------------------------------------------------------------------

@staff_required('cap_donations')
def staff_queue(request):
    status = request.GET.get('status', 'open')
    qs = Donation.objects.select_related('donor', 'partner', 'certificate').prefetch_related('items')
    if status == 'open':
        qs = qs.filter(status__in=('submitted', 'accepted'))
    elif status in dict(Donation.STATUS_CHOICES):
        qs = qs.filter(status=status)
    else:
        status = 'all'
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(donor__username__icontains=q) | Q(partner__name__icontains=q) | Q(certificate__code__icontains=q))
    page = Paginator(qs.order_by('-created_at'), PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'donations/staff_queue.html', {
        'page': page, 'status': status, 'q': q, 'status_choices': Donation.STATUS_CHOICES,
    })


@staff_required('cap_donations')
def staff_detail(request, pk):
    donation = get_object_or_404(Donation.objects.select_related('donor', 'partner'), pk=pk)
    return render(request, 'donations/staff_detail.html', {
        'donation': donation, 'items': donation.items.all(),
        'certificate': getattr(donation, 'certificate', None),
        'receive_form': ReceiveForm(initial={'received_count': donation.claimed_count}),
        'reason_form': ReasonForm(),
    })


def _staff_back(pk):
    return redirect('donations:staff_detail', pk=pk)


@staff_required('cap_donations')
@require_POST
def staff_accept(request, pk):
    donation = get_object_or_404(Donation, pk=pk)
    try:
        d = services.accept_donation(donation, request.user)
    except services.DonationStateError as e:
        messages.error(request, str(e))
        return _staff_back(pk)
    AuditLog.record(actor=request.user, action='accept_donation', target_repr=f'Donation #{pk}', target_id=pk,
                    request=request, old={'status': 'pending'}, new={'status': d.status})
    notify_donation_accepted(d)
    messages.success(request, _('Donation accepted - the donor has been notified.'))
    return _staff_back(pk)


@staff_required('cap_donations')
@require_POST
def staff_reject(request, pk):
    donation = get_object_or_404(Donation, pk=pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, _('Please give a reason for rejecting.'))
        return _staff_back(pk)
    try:
        d = services.reject_donation(donation, request.user, form.cleaned_data['reason'])
    except services.DonationStateError as e:
        messages.error(request, str(e))
        return _staff_back(pk)
    AuditLog.record(actor=request.user, action='reject_donation', target_repr=f'Donation #{pk}', target_id=pk,
                    note=d.rejection_reason, request=request, old={'status': 'pending'}, new={'status': d.status},
                    reason=d.rejection_reason)
    notify_donation_rejected(d)
    messages.success(request, _('Donation rejected - the donor has been notified.'))
    return _staff_back(pk)


@staff_required('cap_donations')
@require_POST
def staff_receive(request, pk):
    donation = get_object_or_404(Donation, pk=pk)
    form = ReceiveForm(request.POST)
    if not form.is_valid():
        messages.error(request, _('Enter how many books were actually received (at least 1).'))
        return _staff_back(pk)
    try:
        cert = services.issue_certificate(donation, request.user, form.cleaned_data['received_count'])
    except services.DonationStateError as e:
        messages.error(request, str(e))
        return _staff_back(pk)
    AuditLog.record(actor=request.user, action='issue_certificate', target_repr=f'{cert.code} (Donation #{pk})',
                    target_id=pk, note=f'{cert.books_count} books verified received', request=request,
                    new={'certificate': cert.code, 'books_count': cert.books_count})
    notify_certificate_issued(cert)
    messages.success(request, _('Marked received - certificate %(code)s issued and sent to the donor.') % {'code': cert.code})
    return _staff_back(pk)


@staff_required('cap_donations')
@require_POST
def staff_revoke(request, code):
    cert = get_object_or_404(DonationCertificate.objects.select_related('donation'), code=_normalize_code(code))
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, _('Please give a reason for revoking.'))
        return _staff_back(cert.donation_id)
    try:
        services.revoke_certificate(cert, form.cleaned_data['reason'])
    except services.DonationStateError as e:
        messages.error(request, str(e))
        return _staff_back(cert.donation_id)
    AuditLog.record(actor=request.user, action='revoke_certificate', target_repr=f'{cert.code} (Donation #{cert.donation_id})',
                    target_id=cert.donation_id, note=cert.revoked_reason, request=request,
                    old={'certificate_active': True}, new={'certificate_active': False}, reason=cert.revoked_reason)
    notify_certificate_revoked(cert)
    messages.success(request, _('Certificate revoked and removed from the Impact totals.'))
    return _staff_back(cert.donation_id)
