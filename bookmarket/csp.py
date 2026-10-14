"""Receives Content-Security-Policy violation reports (see settings.CSP_MODE) and logs them."""
import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .security import rate_limit

logger = logging.getLogger('bookmarket.csp')

MAX_REPORT_BYTES = 8 * 1024
_FIELDS = ('document-uri', 'documentURL', 'violated-directive', 'effective-directive', 'effectiveDirective',
           'blocked-uri', 'blockedURL', 'source-file', 'sourceFile', 'line-number', 'lineNumber', 'disposition')


def _clip(value, n=200):
    return str(value).replace('\n', ' ').replace('\r', ' ')[:n]


@csrf_exempt
@require_POST
@rate_limit('csp-report', 60, 3600, by='ip')
def csp_report(request):
    """Browsers send these without cookies or CSRF tokens, so it is anonymous: capped in size and rate, log-only."""
    if len(request.body) > MAX_REPORT_BYTES:
        return HttpResponseBadRequest('too large')
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return HttpResponseBadRequest('invalid json')
    reports = payload if isinstance(payload, list) else [payload]
    for item in reports[:5]:
        if not isinstance(item, dict):
            continue
        body = item.get('csp-report') or item.get('body') or item
        if isinstance(body, dict):
            summary = {k: _clip(body[k]) for k in _FIELDS if k in body}
            if summary:
                logger.warning('CSP violation: %s', summary)
    return HttpResponse(status=204)
