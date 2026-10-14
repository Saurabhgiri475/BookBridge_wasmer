import json

from django.test import TestCase


class PWATests(TestCase):
    def test_manifest_is_valid_and_installable(self):
        r = self.client.get('/manifest.webmanifest')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/manifest+json')
        m = json.loads(r.content)
        self.assertEqual(m['display'], 'standalone')
        self.assertTrue(m['name'] and m['short_name'])
        self.assertEqual(m['scope'], '/')
        sizes = {(i['sizes'], i['purpose']) for i in m['icons']}
        self.assertIn(('192x192', 'any'), sizes)
        self.assertIn(('512x512', 'any'), sizes)
        self.assertIn(('512x512', 'maskable'), sizes)

    def test_service_worker_served_from_root_with_placeholders_filled(self):
        r = self.client.get('/sw.js')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/javascript')
        self.assertEqual(r['Service-Worker-Allowed'], '/')
        self.assertIn('no-cache', r['Cache-Control'])
        body = r.content.decode()
        self.assertNotIn('__SW_VERSION__', body)
        self.assertNotIn('__PRECACHE__', body)
        self.assertIn('/offline/', body)
        self.assertIn("addEventListener('push'", body)

    def test_offline_page(self):
        r = self.client.get('/offline/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "You're offline")

    def test_base_template_links_manifest(self):
        r = self.client.get('/')
        self.assertContains(r, 'rel="manifest"')
        self.assertContains(r, 'js/pwa.js')
