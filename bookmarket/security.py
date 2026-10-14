"""Small shared security helpers."""
from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request, default, param='next'):
    """
    Returns the user-supplied ?next=/next= value only if it points back at this
    site (same host, and https when the request is https); otherwise `default`
    (a URL name or path). Prevents open redirects like next=https://evil.example
    or next=//evil.example.
    """
    candidate = (request.POST.get(param) or request.GET.get(param) or '').strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return resolve_url(default)


# --- Rate limiting ---------------------------------------------------------------
import functools

from django.conf import settings
from django.http import HttpResponse, JsonResponse


def rate_limit(scope, limit, window_seconds, by='user', json=False):
    """
    View decorator: at most `limit` calls per `window_seconds` per user (or per
    client IP with by='ip'). Counts every call, successful or not, so it also
    caps what a scripted client can spend on paid APIs (OCR, Groq, Twilio...).
    Returns HTTP 429 (+ Retry-After) as JSON when json=True, else plain text.
    Backed by Django's cache - set REDIS_URL in production so every gunicorn
    worker shares the counters (see accounts/throttle.py).
    """
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if not getattr(settings, 'RATE_LIMITING_ENABLED', True):
                return view(request, *args, **kwargs)
            from accounts import throttle  # local import: accounts imports this module's siblings
            ident = request.user.pk if by == 'user' and request.user.is_authenticated else throttle.get_client_ip(request)
            allowed, wait = throttle.hit(scope, ident, limit, window_seconds)
            if allowed:
                return view(request, *args, **kwargs)
            msg = f'Too many requests. Please try again in {throttle.human_wait(wait)}.'
            if json:
                response = JsonResponse({'error': msg, 'valid': False}, status=429)
            else:
                response = HttpResponse(msg, status=429, content_type='text/plain')
            response['Retry-After'] = str(wait)
            return response
        return wrapper
    return decorator
