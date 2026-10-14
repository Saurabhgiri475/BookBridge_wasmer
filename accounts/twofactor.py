"""
Two-factor authentication (TOTP, RFC 6238) for staff accounts.

Staff can refund money, approve partners, open ID documents and resolve disputes,
so a stolen staff password must not be enough. Every staff user enrols an
authenticator app (Google Authenticator, Authy, 1Password ...) and has to enter
a fresh 6-digit code once per session before any staff page, the Django admin or
another person's private documents become reachable (see adminpanel.views.
staff_required and accounts.middleware.StaffTwoFactorAdminMiddleware).

- Secrets are stored encrypted (Fernet, key derived from SECRET_KEY), never as plain text.
- A code can be used only once (replay protection via last_used_step).
- 10 single-use recovery codes are issued at enrolment, stored only as keyed hashes.
- Lost the device and the recovery codes? `manage.py reset_staff_2fa <username>` (console access).
"""
import base64
import hmac
import secrets
import struct
import time
from functools import wraps

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_WINDOW = 1  # accept the previous and next 30-second step too, for clock drift
RECOVERY_CODE_COUNT = 10
SESSION_KEY = 'staff_2fa'
ISSUER = 'BookBridge'


class SecretUnreadable(Exception):
    """The stored secret can't be decrypted (SECRET_KEY changed). Reset with reset_staff_2fa."""


# --- TOTP ---------------------------------------------------------------------------------

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode()  # 160 bits, 32 base32 chars


def totp_at(secret_b32, step):
    key = base64.b32decode(secret_b32, casefold=True)
    digest = hmac.new(key, struct.pack('>Q', step), 'sha1').digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return f'{value:0{TOTP_DIGITS}d}'


def normalise_code(raw):
    return ''.join(ch for ch in (raw or '') if ch.isdigit())


def verify_totp(secret_b32, raw_code, last_used_step=0, now=None):
    """Returns the matching time step (an int) or None. A step at or before
    last_used_step is rejected, so a code that was already accepted can't be replayed."""
    code = normalise_code(raw_code)
    if len(code) != TOTP_DIGITS:
        return None
    current = int((time.time() if now is None else now) // TOTP_PERIOD)
    matched = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if constant_time_compare(totp_at(secret_b32, step), code) and step > last_used_step:
            matched = step
    return matched


def provisioning_uri(secret_b32, account_name):
    from urllib.parse import quote
    return (f'otpauth://totp/{quote(ISSUER)}:{quote(account_name)}'
            f'?secret={secret_b32}&issuer={quote(ISSUER)}&digits={TOTP_DIGITS}&period={TOTP_PERIOD}')


def qr_svg(data):
    """Inline SVG QR code for the authenticator app (no external service, nothing leaves the server)."""
    import segno
    return segno.make(data, error='m').svg_inline(scale=5, border=2, dark='#111111', light='#ffffff')


# --- Secret encryption --------------------------------------------------------------------

def _fernet():
    raw = salted_hmac('bookbridge.2fa.secret.v1', 'fernet-key', algorithm='sha256').digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(secret_b32):
    return _fernet().encrypt(secret_b32.encode()).decode()


def decrypt_secret(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretUnreadable from exc


# --- Recovery codes -----------------------------------------------------------------------

def normalise_recovery(raw):
    return ''.join(ch for ch in (raw or '').lower() if ch.isalnum())


def make_recovery_codes(n=RECOVERY_CODE_COUNT):
    out = []
    for _ in range(n):
        token = secrets.token_hex(5)  # 40 bits each; single-use and rate-limited
        out.append(f'{token[:5]}-{token[5:]}')
    return out


def hash_recovery_code(user_pk, code):
    return salted_hmac('bookbridge.2fa.recovery.v1', f'{user_pk}:{normalise_recovery(code)}', algorithm='sha256').hexdigest()


# --- Per-session state --------------------------------------------------------------------

def required():
    return bool(getattr(settings, 'STAFF_2FA_REQUIRED', True))


def is_verified(request):
    data = request.session.get(SESSION_KEY)
    user = request.user
    if not (data and user.is_authenticated and data.get('uid') == user.pk):
        return False
    max_age = getattr(settings, 'STAFF_2FA_MAX_AGE_SECONDS', 12 * 3600)
    return (time.time() - data.get('at', 0)) < max_age


def mark_verified(request):
    request.session[SESSION_KEY] = {'uid': request.user.pk, 'at': int(time.time())}


def clear_verified(request):
    request.session.pop(SESSION_KEY, None)


def staff_ok(request, capability=None):
    """True for a staff user who has passed 2FA this session (or when 2FA is switched off) and,
    if a capability such as 'cap_partners' is given, whose role includes it."""
    user = request.user
    if not (user.is_authenticated and user.is_staff and (not required() or is_verified(request))):
        return False
    return capability is None or user.has_perm(f'adminpanel.{capability}')


def has_confirmed_device(user):
    from .models import StaffTOTPDevice
    return StaffTOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def challenge_redirect(request):
    """Send an unverified staff user to enrol (first time) or enter a code, then come back here."""
    from urllib.parse import urlencode

    from django.shortcuts import redirect
    from django.urls import reverse
    target = 'accounts:two_factor_verify' if has_confirmed_device(request.user) else 'accounts:two_factor_setup'
    return redirect(f"{reverse(target)}?{urlencode({'next': request.get_full_path()})}")


def staff_2fa_gate(view_func):
    """Inner gate used by adminpanel.views.staff_required (after login + is_staff were checked)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if required() and not is_verified(request):
            return challenge_redirect(request)
        return view_func(request, *args, **kwargs)
    return wrapper
