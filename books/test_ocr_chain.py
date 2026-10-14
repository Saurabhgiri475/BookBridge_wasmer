"""
Tests for the cover-scan OCR chain in books/ocr_utils.scan_cover_image():
OCR.space -> Gemini -> (Google Vision) -> Tesseract.

No real keys or network: requests.post and the Tesseract call are mocked.
"""
import io
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from PIL import Image

from . import ocr_utils
from .gemini_ocr import GeminiOcrError, run_gemini_ocr
from .ocr_image import OcrEngineError, prepare_image
from .ocrspace_ocr import OcrSpaceError, run_ocr_space

KEYS = dict(OCR_SPACE_API_KEY='ocr-key', GEMINI_API_KEY='gem-key', GOOGLE_VISION_API_KEY='')
COVER_TEXT = 'Engineering Physics\nby R K Gaur\nDhanpat Rai\nISBN 978-81-7700-123-4'


def _img_bytes(size=(1200, 1600), fmt='PNG'):
    buf = io.BytesIO()
    Image.effect_noise(size, 60).convert('RGB').save(buf, fmt)
    return buf.getvalue()


def _upload():
    f = io.BytesIO(_img_bytes((300, 400)))
    f.name = 'cover.png'
    return f


def _resp(status=200, body=None):
    r = Mock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.text = json.dumps(body or {})
    return r


def _ocrspace_ok(text=COVER_TEXT, lines=None):
    result = {'ParsedText': text}
    if lines is not None:
        result['TextOverlay'] = {'Lines': [{'LineText': t, 'MaxHeight': h, 'Words': []} for t, h in lines]}
    return _resp(200, {'IsErroredOnProcessing': False, 'ParsedResults': [result]})


def _gemini_ok(**fields):
    payload = {'title': 'Engineering Physics', 'author': 'R K Gaur', 'publisher': '',
               'isbn': '978-81-7700-123-4', 'edition': '', 'publication_year': '', 'raw_text': 'x'}
    payload.update(fields)
    return _resp(200, {'candidates': [{'content': {'parts': [{'text': json.dumps(payload)}]}}]})


def _route(space=None, gemini=None):
    """Both engines call the same requests.post, so patch it once and route by URL.
    Each of space/gemini is a response, or an exception instance to raise."""
    calls = {'space': 0, 'gemini': 0}

    def fake_post(url, *a, **kw):
        which = 'space' if 'ocr.space' in url else 'gemini'
        calls[which] += 1
        out = space if which == 'space' else gemini
        if isinstance(out, Exception):
            raise out
        return out

    patcher = patch('requests.post', side_effect=fake_post)
    return patcher, calls


class PrepareImageTests(SimpleTestCase):
    def test_big_image_is_compressed_under_limit(self):
        data, mime = prepare_image(_img_bytes((3000, 4000)), max_bytes=900 * 1024)
        self.assertLessEqual(len(data), 900 * 1024)
        self.assertEqual(mime, 'image/jpeg')

    def test_garbage_bytes_raise(self):
        with self.assertRaises(OcrEngineError):
            prepare_image(b'not an image')


class EngineTests(SimpleTestCase):
    def test_ocr_space_requires_key(self):
        with self.assertRaises(OcrSpaceError):
            run_ocr_space(_img_bytes((200, 200)), '')

    @patch('books.ocrspace_ocr.requests.post')
    def test_ocr_space_parses_text_and_sends_key_in_header(self, post):
        post.return_value = _ocrspace_ok()
        self.assertIn('Engineering Physics', run_ocr_space(_img_bytes((200, 200)), 'k'))
        self.assertEqual(post.call_args.kwargs['headers'], {'apikey': 'k'})

    @patch('books.ocrspace_ocr.requests.post')
    def test_ocr_space_error_flag_raises(self, post):
        post.return_value = _resp(200, {'IsErroredOnProcessing': True, 'ErrorMessage': ['File size exceeds limit']})
        with self.assertRaises(OcrSpaceError):
            run_ocr_space(_img_bytes((200, 200)), 'k')

    @patch('books.gemini_ocr.requests.post')
    def test_gemini_parses_json_even_when_fenced(self, post):
        body = {'candidates': [{'content': {'parts': [{'text': '```json\n{"title": "T", "raw_text": "T"}\n```'}]}}]}
        post.return_value = _resp(200, body)
        self.assertEqual(run_gemini_ocr(_img_bytes((200, 200)), 'k')['title'], 'T')
        self.assertNotIn('key=', post.call_args.args[0])  # key must not be in the URL

    @patch('books.gemini_ocr.requests.post')
    def test_gemini_quota_error_raises(self, post):
        post.return_value = _resp(429, {})
        with self.assertRaises(GeminiOcrError):
            run_gemini_ocr(_img_bytes((200, 200)), 'k')


class NoAiMixin:
    """These tests are about the OCR-engine fallback order only, so switch the
    text->fields AI step off (it has its own tests in AiParseTests below)."""
    def setUp(self):
        super().setUp()
        p = patch('books.ocr_utils._enrich_with_ai', side_effect=lambda result, layout_lines=None: result)
        p.start()
        self.addCleanup(p.stop)


@override_settings(**KEYS)
class ChainTests(NoAiMixin, SimpleTestCase):
    def _tess(self):
        return patch('books.ocr_utils._run_tesseract_ocr', return_value='Tesseract Title\nby Some One')

    def test_ocr_space_wins_and_gemini_not_called(self):
        patcher, calls = _route(space=_ocrspace_ok(), gemini=_gemini_ok())
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'OCR.space')
        self.assertEqual(r['author'], 'R K Gaur')
        self.assertEqual(calls['gemini'], 0)

    def test_falls_back_to_gemini_when_ocr_space_fails(self):
        patcher, _ = _route(space=_resp(500), gemini=_gemini_ok())
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'Google Gemini')
        self.assertEqual(r['isbn'], '9788177001234')

    def test_empty_ocr_space_text_also_falls_through(self):
        patcher, _ = _route(space=_ocrspace_ok(text='   '), gemini=_gemini_ok())
        with patcher:
            self.assertEqual(ocr_utils.scan_cover_image(_upload())['source'], 'Google Gemini')

    def test_falls_back_to_tesseract_when_both_fail(self):
        import requests
        patcher, _ = _route(space=requests.ConnectionError('down'), gemini=_resp(429))
        with patcher, self._tess():
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'Tesseract (offline)')
        self.assertEqual(r['title'], 'Tesseract Title')

    @override_settings(OCR_SPACE_API_KEY='', GEMINI_API_KEY='')
    def test_no_keys_goes_straight_to_tesseract(self):
        patcher, calls = _route(space=_ocrspace_ok(), gemini=_gemini_ok())
        with patcher, self._tess():
            self.assertEqual(ocr_utils.scan_cover_image(_upload())['source'], 'Tesseract (offline)')
        self.assertEqual(calls, {'space': 0, 'gemini': 0})

    def test_weak_online_result_returned_if_tesseract_missing(self):
        patcher, _ = _route(space=_ocrspace_ok(text='1234 5678'), gemini=_resp(500))  # text, but no title/author/isbn
        with patcher, patch('books.ocr_utils._run_tesseract_ocr', side_effect=RuntimeError('tesseract not installed')):
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'OCR.space')
        self.assertTrue(r['confidence_note'])

    def test_gemini_ignores_invalid_isbn(self):
        patcher, _ = _route(space=_resp(500), gemini=_gemini_ok(isbn='12345'))
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertIsNone(r['isbn'])


# A cover with NO "by ..." line - the old heuristic dumped every line into the title.
NO_BY_TEXT = 'Concepts of Physics\nVolume 1\nH C Verma\nBharati Bhawan\nFor JEE Main and Advanced\nRevised Edition'
NO_BY_LINES = [('Concepts of Physics', 90), ('Volume 1', 80), ('H C Verma', 40),
               ('Bharati Bhawan', 25), ('For JEE Main and Advanced', 22), ('Revised Edition', 20)]


class TitleSplitTests(NoAiMixin, SimpleTestCase):
    def test_plain_text_title_is_capped_and_author_found(self):
        text = '\n'.join(['Alpha Beta', 'Gamma Delta', 'Epsilon Zeta', 'Eta Theta', 'Iota Kappa', 'Lambda Mu'])
        r = ocr_utils.parse_fields_from_text(text)
        self.assertEqual(r['title'], 'Alpha Beta Gamma Delta Epsilon Zeta')  # 3 lines max, not all 6

    def test_layout_uses_biggest_type_for_title(self):
        r = ocr_utils.parse_fields_from_layout(NO_BY_TEXT, [{'text': t, 'height': h} for t, h in NO_BY_LINES])
        self.assertEqual(r['title'], 'Concepts of Physics Volume 1')
        self.assertEqual(r['author'], 'H C Verma')

    def test_layout_missing_falls_back_to_text_parser(self):
        r = ocr_utils.parse_fields_from_layout(COVER_TEXT, [])
        self.assertEqual(r['author'], 'R K Gaur')

    @override_settings(**KEYS)
    def test_ocr_space_overlay_gives_clean_title_end_to_end(self):
        patcher, _ = _route(space=_ocrspace_ok(NO_BY_TEXT, NO_BY_LINES), gemini=_gemini_ok())
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'OCR.space')
        self.assertEqual(r['title'], 'Concepts of Physics Volume 1')
        self.assertNotIn('Bharati', r['title'])

    @override_settings(**KEYS)
    def test_dumped_title_falls_through_to_gemini(self):
        dump = '\n'.join(f'Some long cover line number {n} here' for n in range(12))
        patcher, _ = _route(space=_ocrspace_ok(dump), gemini=_gemini_ok())
        with patcher:
            self.assertEqual(ocr_utils.scan_cover_image(_upload())['source'], 'Google Gemini')


# ---------- OCR text -> AI -> form fields ----------

def _ai_reply(**fields):
    payload = {'title': '', 'author': '', 'publisher': '', 'isbn': '', 'edition': '', 'publication_year': ''}
    payload.update(fields)
    return _resp(200, {'candidates': [{'content': {'parts': [{'text': json.dumps(payload)}]}}]})


class IsbnChecksumTests(SimpleTestCase):
    def test_valid_and_invalid(self):
        from .ai_cover_parser import isbn_is_valid
        self.assertTrue(isbn_is_valid('9780306406157'))
        self.assertTrue(isbn_is_valid('0306406152'))
        self.assertFalse(isbn_is_valid('9780306406158'))
        self.assertFalse(isbn_is_valid('12345'))


@override_settings(OCR_SPACE_API_KEY='ocr-key', GEMINI_API_KEY='gem-key', GROQ_API_KEY='', GOOGLE_VISION_API_KEY='')
class AiParseTests(SimpleTestCase):
    MESSY = 'CONCEPTS OF PHVSICS\nH C Verma\nBharati Bhawan Publishers\n3rd Edn\nISBN 978-0-306-40615-7\nFirst published 2019\nMRP Rs 450'

    def test_ai_sorts_ocr_text_into_every_field(self):
        ai = _ai_reply(title='Concepts of Physics', author='H C Verma', publisher='Bharati Bhawan',
                       isbn='978-0-306-40615-7', edition='3rd Edition', publication_year='2019')
        patcher, calls = _route(space=_ocrspace_ok(self.MESSY), gemini=ai)
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'OCR.space + Gemini AI')
        self.assertEqual(
            (r['title'], r['author'], r['publisher'], r['isbn'], r['edition'], r['publication_year']),
            ('Concepts of Physics', 'H C Verma', 'Bharati Bhawan', '9780306406157', '3rd Edition', 2019))
        self.assertEqual(calls, {'space': 1, 'gemini': 1})

    def test_ai_gets_the_ocr_text_not_the_image(self):
        patcher, _ = _route(space=_ocrspace_ok(self.MESSY), gemini=_ai_reply(title='X'))
        with patcher as post:
            ocr_utils.scan_cover_image(_upload())
        gemini_call = [c for c in post.call_args_list if 'ocr.space' not in c.args[0]][0]
        sent = json.dumps(gemini_call.kwargs['json'])
        self.assertIn('CONCEPTS OF PHVSICS', sent)
        self.assertNotIn('inlineData', sent)

    def test_ai_blank_fields_keep_heuristic_values(self):
        patcher, _ = _route(space=_ocrspace_ok(), gemini=_ai_reply(publisher='Dhanpat Rai'))
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['publisher'], 'Dhanpat Rai')
        self.assertEqual(r['author'], 'R K Gaur')  # from the regex parser

    def test_ai_invented_isbn_is_dropped(self):
        patcher, _ = _route(space=_ocrspace_ok('Some Title\nby A B Writer'), gemini=_ai_reply(
            title='Some Title', isbn='9780306406158'))
        with patcher:
            self.assertIsNone(ocr_utils.scan_cover_image(_upload())['isbn'])

    def test_ai_outage_falls_back_to_plain_heuristics(self):
        patcher, _ = _route(space=_ocrspace_ok(), gemini=_resp(500))
        with patcher:
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'OCR.space')
        self.assertEqual(r['author'], 'R K Gaur')

    @override_settings(GEMINI_API_KEY='', GROQ_API_KEY='groq-key')
    def test_groq_is_used_when_only_groq_key_set(self):
        groq = _resp(200, {'choices': [{'message': {'content': json.dumps({'title': 'Concepts of Physics'})}}]})
        with patch('requests.post', side_effect=lambda url, *a, **kw: _ocrspace_ok(self.MESSY) if 'ocr.space' in url else groq):
            r = ocr_utils.scan_cover_image(_upload())
        self.assertEqual(r['source'], 'OCR.space + Groq AI')
        self.assertEqual(r['title'], 'Concepts of Physics')

    def test_layout_sizes_are_sent_to_ai(self):
        from .ai_cover_parser import format_text_for_ai
        out = format_text_for_ai('', [{'text': 'Big Title', 'height': 90}, {'text': 'Author', 'height': 45}])
        self.assertEqual(out, '[size 100] Big Title\n[size 50] Author')
