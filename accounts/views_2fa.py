from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache

from adminpanel.models import AuditLog
from adminpanel.views import staff_required
from bookmarket.security import safe_next_url

from . import throttle, twofactor
from .models import StaffRecoveryCode, StaffTOTPDevice

USER_MAX_FAILURES = 5
IP_MAX_FAILURES = 20
FAILURE_WINDOW = 15 * 60


def _staff_only(view):
    """Logged-in staff, but without the 2FA gate (these are the pages that perform it)."""
    return never_cache(login_required(user_passes_test(lambda u: u.is_staff)(view)))


def _blocked_wait(request):
    """Seconds to wait if too many wrong codes were entered recently, else 0."""
    if not settings.RATE_LIMITING_ENABLED:
        return 0
    for scope, ident, limit in (('2fa-fail-user', request.user.pk, USER_MAX_FAILURES),
                                ('2fa-fail-ip', throttle.get_client_ip(request), IP_MAX_FAILURES)):
        if throttle.peek(scope, ident, limit, FAILURE_WINDOW):
            return FAILURE_WINDOW
    return 0


def _count_failure(request):
    if settings.RATE_LIMITING_ENABLED:
        throttle.hit('2fa-fail-user', request.user.pk, USER_MAX_FAILURES, FAILURE_WINDOW)
        throttle.hit('2fa-fail-ip', throttle.get_client_ip(request), IP_MAX_FAILURES, FAILURE_WINDOW)


def _too_many(request):
    messages.error(request, _('Too many wrong codes. Wait about 15 minutes and try again.'))
    AuditLog.record(actor=request.user, action='staff_2fa_locked', target_repr=request.user.get_username(),
                    target_id=request.user.pk, note='Too many wrong 2FA codes.', request=request)


def _issue_recovery_codes(user):
    codes = twofactor.make_recovery_codes()
    StaffRecoveryCode.objects.filter(user=user).delete()
    StaffRecoveryCode.objects.bulk_create(
        [StaffRecoveryCode(user=user, code_hash=twofactor.hash_recovery_code(user.pk, c)) for c in codes])
    return codes


def _secret_of(device):
    try:
        return twofactor.decrypt_secret(device.secret_encrypted)
    except twofactor.SecretUnreadable:
        return None


@_staff_only
def setup(request):
    """First-time enrolment: scan the QR code, prove it works with one code, get recovery codes."""
    user = request.user
    device = StaffTOTPDevice.objects.filter(user=user).first()
    if device and device.confirmed_at:
        return redirect('accounts:two_factor_manage' if twofactor.is_verified(request) else 'accounts:two_factor_verify')

    secret = _secret_of(device) if device else None
    if secret is None:  # no device yet (or an unreadable half-finished one): start fresh
        secret = twofactor.new_secret()
        StaffTOTPDevice.objects.update_or_create(user=user, defaults={'secret_encrypted': twofactor.encrypt_secret(secret), 'confirmed_at': None, 'last_used_step': 0})
        device = StaffTOTPDevice.objects.get(user=user)

    next_url = safe_next_url(request, reverse('adminpanel:dashboard'))
    if request.method == 'POST':
        if _blocked_wait(request):
            _too_many(request)
        else:
            step = twofactor.verify_totp(secret, request.POST.get('code', ''), device.last_used_step)
            if step is None:
                _count_failure(request)
                messages.error(request, _("That code didn't match. Check the code in your app and your phone's clock, then try again."))
            else:
                with transaction.atomic():
                    device.confirmed_at = timezone.now()
                    device.last_used_step = step
                    device.save(update_fields=['confirmed_at', 'last_used_step'])
                    codes = _issue_recovery_codes(user)
                    AuditLog.record(actor=user, action='staff_2fa_enrolled', target_repr=user.get_username(), target_id=user.pk, request=request,
                                    old={'two_factor': False}, new={'two_factor': True})
                request.session.cycle_key()
                twofactor.mark_verified(request)
                return render(request, 'accounts/two_factor_recovery_codes.html', {'codes': codes, 'next_url': next_url, 'first_time': True})

    uri = twofactor.provisioning_uri(secret, user.get_username())
    return render(request, 'accounts/two_factor_setup.html', {
        'qr_svg': twofactor.qr_svg(uri), 'secret_grouped': ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        'next_url': next_url,
    })


@_staff_only
def verify(request):
    """The per-session check: a current authenticator code, or one single-use recovery code."""
    user = request.user
    device = StaffTOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    next_url = safe_next_url(request, reverse('adminpanel:dashboard'))
    if device is None:
        return redirect(f"{reverse('accounts:two_factor_setup')}?next={next_url}")
    if request.method == 'GET' and twofactor.is_verified(request):
        return redirect(next_url)

    if request.method == 'POST':
        if _blocked_wait(request):
            _too_many(request)
            return render(request, 'accounts/two_factor_verify.html', {'next_url': next_url})
        raw = request.POST.get('code', '').strip()
        ok = False
        if raw.isdigit() or (twofactor.normalise_code(raw) == twofactor.normalise_recovery(raw) and len(raw) <= 7):
            secret = _secret_of(device)
            if secret is None:
                messages.error(request, _('Your 2FA secret can no longer be read. Ask a server admin to run reset_staff_2fa for your account.'))
                return render(request, 'accounts/two_factor_verify.html', {'next_url': next_url})
            step = twofactor.verify_totp(secret, raw, device.last_used_step)
            # Atomic compare-and-set: two requests racing with the same code can't both win.
            ok = bool(step) and StaffTOTPDevice.objects.filter(pk=device.pk, last_used_step__lt=step).update(last_used_step=step) == 1
        else:
            code_hash = twofactor.hash_recovery_code(user.pk, raw)
            used = StaffRecoveryCode.objects.filter(user=user, code_hash=code_hash, used_at__isnull=True).update(used_at=timezone.now())
            if used == 1:
                ok = True
                left = StaffRecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
                AuditLog.record(actor=user, action='staff_2fa_recovery_used', target_repr=user.get_username(), target_id=user.pk, note=f'{left} recovery codes left.', request=request)
                messages.warning(request, _('You signed in with a recovery code. %(left)s left - generate new ones from the 2FA page if you are running low.') % {'left': left})
        if ok:
            if settings.RATE_LIMITING_ENABLED:
                throttle.reset('2fa-fail-user', user.pk, FAILURE_WINDOW)
            request.session.cycle_key()
            twofactor.mark_verified(request)
            return redirect(next_url)
        _count_failure(request)
        messages.error(request, _("That code didn't work. Enter the 6-digit code from your authenticator app, or a recovery code."))
    return render(request, 'accounts/two_factor_verify.html', {'next_url': next_url})


@staff_required
def manage(request):
    """Status page, and a way to replace the recovery codes (needs a fresh authenticator code)."""
    user = request.user
    device = StaffTOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    if device is None:
        return redirect('accounts:two_factor_setup')
    remaining = StaffRecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
    if request.method == 'POST':
        if _blocked_wait(request):
            _too_many(request)
        else:
            secret = _secret_of(device)
            step = twofactor.verify_totp(secret, request.POST.get('code', ''), device.last_used_step) if secret else None
            if step and StaffTOTPDevice.objects.filter(pk=device.pk, last_used_step__lt=step).update(last_used_step=step) == 1:
                codes = _issue_recovery_codes(user)
                AuditLog.record(actor=user, action='staff_2fa_codes_regenerated', target_repr=user.get_username(), target_id=user.pk, request=request)
                return render(request, 'accounts/two_factor_recovery_codes.html', {'codes': codes, 'next_url': reverse('adminpanel:dashboard'), 'first_time': False})
            _count_failure(request)
            messages.error(request, _("That code didn't match."))
    return render(request, 'accounts/two_factor_manage.html', {'device': device, 'remaining': remaining})
