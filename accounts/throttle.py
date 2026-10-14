"""
Abuse protection for signup: rate limiting, honeypot + timing check, and an
optional CAPTCHA (Cloudflare Turnstile - free, no puzzle for most people).

Rate limits use Django's cache, so they are only as shared as the cache is:
with the default per-process LocMemCache every gunicorn worker keeps its own
counters (the effective limit becomes limit x workers). Set REDIS_URL (or
CACHE_BACKEND=db plus `manage.py createcachetable`) in production - see
settings.CACHES.
"""
import hashlib
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, TimestampSigner

logger = logging.getLogger(__name__)

HONEYPOT_FIELD = 'website'          # real users never see or fill this one
FORM_TOKEN_FIELD = 'form_token'     # signed render-time stamp, for the too-fast check
MIN_FILL_SECONDS = 3                # a human can't read + fill a signup form faster than this
MAX_TOKEN_AGE_SECONDS = 60 * 60 * 6
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'

_signer = TimestampSigner(salt='bookbridge.signup.form')


def get_client_ip(request):
    """Real client IP. X-Forwarded-For is only honoured when NUM_PROXIES > 0
    (how many trusted reverse proxies sit in front of Django), because the
    header is trivially spoofable otherwise."""
    num_proxies = getattr(settings, 'NUM_PROXIES', 0)
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if num_proxies and forwarded:
        hops = [h.strip() for h in forwarded.split(',') if h.strip()]
        if len(hops) >= num_proxies:
            return hops[-num_proxies]
    return request.META.get('REMOTE_ADDR', '') or 'unknown'


def _key(scope, ident):
    return 'throttle:%s:%s' % (scope, hashlib.sha256(str(ident).encode()).hexdigest()[:32])


def hit(scope, ident, limit, window_seconds):
    """Count one event. Returns (allowed, retry_after_seconds). Fixed window."""
    key = _key(scope, ident)
    now = int(time.time())
    window_start = now - (now % window_seconds)
    window_key = f'{key}:{window_start}'
    cache.add(window_key, 0, timeout=window_seconds + 5)
    try:
        count = cache.incr(window_key)
    except ValueError:  # expired between add() and incr()
        cache.set(window_key, 1, timeout=window_seconds + 5)
        count = 1
    allowed = count <= limit
    return allowed, max(1, window_start + window_seconds - now)


def peek(scope, ident, limit, window_seconds):
    """True if the budget for this window is already used up (does not count a hit)."""
    now = int(time.time())
    window_start = now - (now % window_seconds)
    return (cache.get(f'{_key(scope, ident)}:{window_start}') or 0) >= limit


def reset(scope, ident, window_seconds):
    """Forget this window's count (e.g. after a successful login)."""
    now = int(time.time())
    window_start = now - (now % window_seconds)
    cache.delete(f'{_key(scope, ident)}:{window_start}')


def human_wait(seconds):
    minutes = -(-seconds // 60)
    return 'a minute' if minutes <= 1 else f'{minutes} minutes'


# --- Honeypot + timing ---------------------------------------------------------

def make_form_token():
    return _signer.sign(str(int(time.time())))


def looks_like_bot(post):
    """True when the hidden honeypot was filled, or the form came back
    implausibly fast / with a missing, forged or ancient token."""
    if (post.get(HONEYPOT_FIELD) or '').strip():
        return True
    token = post.get(FORM_TOKEN_FIELD, '')
    try:
        issued = int(_signer.unsign(token, max_age=MAX_TOKEN_AGE_SECONDS).split(':')[0])
    except (BadSignature, ValueError):
        return True
    return time.time() - issued < MIN_FILL_SECONDS


# --- CAPTCHA (Cloudflare Turnstile) -----------------------------------------------

def captcha_enabled():
    return bool(getattr(settings, 'TURNSTILE_SITE_KEY', '') and getattr(settings, 'TURNSTILE_SECRET_KEY', ''))


def verify_captcha(request):
    """True if captcha is not configured, or Turnstile accepted the response.
    Fails closed: if Cloudflare can't be reached, the signup is not let through
    (set TURNSTILE_SECRET_KEY to blank to switch the captcha off)."""
    if not captcha_enabled():
        return True
    token = request.POST.get('cf-turnstile-response', '')
    if not token:
        return False
    try:
        resp = requests.post(
            TURNSTILE_VERIFY_URL,
            data={'secret': settings.TURNSTILE_SECRET_KEY, 'response': token, 'remoteip': get_client_ip(request)},
            timeout=5,
        )
        return bool(resp.json().get('success'))
    except (requests.RequestException, ValueError):
        logger.warning('Turnstile verification failed to reach Cloudflare', exc_info=True)
        return False
