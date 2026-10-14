import json
import re
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

TEMPLATES = Path(settings.BASE_DIR) / 'templates'


class CspHeaderTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_report_only_header_has_nonce_and_strict_script_src(self):
        resp = self.client.get(reverse('books:home'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('Content-Security-Policy', resp.headers)  # default mode never blocks
        policy = resp['Content-Security-Policy-Report-Only']
        script_src = next(d for d in policy.split(';') if d.strip().startswith('script-src'))
        self.assertIn("'nonce-", script_src)
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn('report-uri /csp-report/', policy)

    def test_inline_scripts_carry_the_same_nonce_as_the_header(self):
        resp = self.client.get(reverse('books:home'))
        nonce = re.search(r"'nonce-([^']+)'", resp['Content-Security-Policy-Report-Only']).group(1)
        html = resp.content.decode()
        self.assertIn(f'nonce="{nonce}"', html)
        inline = re.findall(r'<script(?![^>]*\bsrc=)([^>]*)>', html)
        self.assertTrue(inline)
        for attrs in inline:
            self.assertIn(f'nonce="{nonce}"', attrs)

    def test_each_request_gets_a_fresh_nonce(self):
        a = self.client.get(reverse('books:home'))['Content-Security-Policy-Report-Only']
        b = self.client.get(reverse('books:home'))['Content-Security-Policy-Report-Only']
        self.assertNotEqual(re.search(r"'nonce-([^']+)'", a).group(1), re.search(r"'nonce-([^']+)'", b).group(1))


class CspReportEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('csp_report')

    def _post(self, body, content_type='application/csp-report'):
        return self.client.post(self.url, data=body, content_type=content_type)

    def test_accepts_a_report_and_logs_it(self):
        report = {'csp-report': {'document-uri': 'https://x/y', 'violated-directive': 'script-src', 'blocked-uri': 'inline'}}
        with self.assertLogs('bookmarket.csp', level='WARNING') as logs:
            resp = self._post(json.dumps(report))
        self.assertEqual(resp.status_code, 204)
        self.assertIn('script-src', logs.output[0])

    def test_rejects_get_oversized_and_invalid_bodies(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self._post('{"a": "' + 'x' * 9000 + '"}').status_code, 400)
        self.assertEqual(self._post('not json').status_code, 400)

    def test_log_injection_is_neutralised(self):
        report = {'csp-report': {'blocked-uri': 'a\nCSP violation: forged ' + 'z' * 500}}
        with self.assertLogs('bookmarket.csp', level='WARNING') as logs:
            self._post(json.dumps(report))
        self.assertNotIn('\n', logs.records[0].getMessage())
        self.assertLess(len(logs.records[0].getMessage()), 400)

    def test_is_rate_limited_per_ip(self):
        codes = [self._post('{}').status_code for _ in range(62)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 204)


class TemplateSafetyRegressionTests(TestCase):
    """Cheap static guards so the CSP/XSS fixes can't quietly regress."""

    def _templates(self):
        return list(TEMPLATES.rglob('*.html'))

    def test_no_inline_event_handlers(self):
        pattern = re.compile(r'\son[a-z]+\s*=\s*["\']', re.I)
        offenders = [str(p.relative_to(TEMPLATES)) for p in self._templates() if pattern.search(p.read_text())]
        self.assertEqual(offenders, [], 'inline on*= handlers are blocked by CSP; use data-confirm / data-autosubmit')

    def test_every_inline_script_has_a_nonce(self):
        bad = []
        for p in self._templates():
            for attrs in re.findall(r'<script(?![^>]*\bsrc=)([^>]*)>', p.read_text()):
                if 'nonce=' not in attrs and 'application/json' not in attrs and 'ld+json' not in attrs:
                    bad.append(str(p.relative_to(TEMPLATES)))
        self.assertEqual(bad, [])

    def test_server_data_is_escaped_before_going_into_innerhtml_templates(self):
        raw = re.compile(r'\$\{\s*(?:data|d|l|f)\.[A-Za-z_]+[^}]*\}')
        bad = []
        for p in self._templates():
            for line in p.read_text().splitlines():
                for m in raw.finditer(line):
                    expr = m.group(0)
                    if 'bbEsc(' in expr or 'esc(' in expr:
                        continue
                    if re.fullmatch(r"\$\{[^?}]*\?\s*'[^'$]*'\s*:\s*'[^'$]*'\s*\}", expr):
                        continue  # a comparison choosing between two fixed literals (e.g. plural 's') carries no data
                    bad.append((p.name, expr))
        self.assertEqual(bad, [])
