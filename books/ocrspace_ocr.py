"""
OCR.space OCR for the "Scan Book Cover" feature (first engine in the chain).

Free tier: request a key by email at https://ocr.space/ocrapi/freekey - no
credit card, no billing account (about 25,000 requests/month, 1 MB max file
size - which is why the image is compressed first, see ocr_image.py).

Plain HTTPS POST via requests, same approach as vision_ocr.py - no SDK.
Returns raw text, so the same heuristics in ocr_utils.parse_fields_from_text
turn it into fields.
"""
import logging

import requests

from .ocr_image import OcrEngineError, prepare_image

logger = logging.getLogger(__name__)

OCR_SPACE_URL = 'https://api.ocr.space/parse/image'
REQUEST_TIMEOUT = 20  # the free endpoint can be slow; still fail well before the browser gives up
FREE_TIER_MAX_BYTES = 1024 * 1024


class OcrSpaceError(OcrEngineError):
    """Any OCR.space failure - the caller falls through to the next engine."""


def run_ocr_space(image_bytes: bytes, api_key: str) -> str:
    """Plain text only (kept for callers that don't care about layout)."""
    return run_ocr_space_detailed(image_bytes, api_key)['text']


def run_ocr_space_detailed(image_bytes: bytes, api_key: str) -> dict:
    """Returns {'text': str, 'lines': [{'text': str, 'height': float}, ...]} in reading order.
    'lines' is empty if OCR.space didn't send layout data - callers must cope."""
    if not api_key:
        raise OcrSpaceError('No OCR.space API key configured.')
    if not image_bytes:
        raise OcrSpaceError('No image data to send.')

    data, mime = prepare_image(image_bytes, max_bytes=int(FREE_TIER_MAX_BYTES * 0.9))

    try:
        resp = requests.post(
            OCR_SPACE_URL,
            headers={'apikey': api_key},
            files={'file': ('cover.jpg', data, mime)},
            data={
                'language': 'eng',
                'OCREngine': '2',        # engine 2 copes better with stylised/odd fonts
                'scale': 'true',
                'isOverlayRequired': 'true',     # gives us each line's text height -> the biggest text is the title
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise OcrSpaceError(f'Could not reach OCR.space: {exc}') from exc

    if resp.status_code != 200:
        logger.warning('OCR.space returned %s: %s', resp.status_code, resp.text[:300])
        raise OcrSpaceError(f'OCR.space error (status {resp.status_code}).')

    try:
        body = resp.json()
    except ValueError as exc:
        raise OcrSpaceError('OCR.space returned an unreadable response.') from exc

    if body.get('IsErroredOnProcessing'):
        msg = body.get('ErrorMessage') or body.get('ErrorDetails') or 'unknown error'
        if isinstance(msg, list):
            msg = '; '.join(str(m) for m in msg)
        raise OcrSpaceError(f'OCR.space could not process the image: {msg}')

    results = body.get('ParsedResults') or []
    text = '\n'.join((r.get('ParsedText') or '') for r in results).strip()
    if not text:
        raise OcrSpaceError('OCR.space found no text in the image.')

    lines = []
    for r in results:
        for ln in ((r.get('TextOverlay') or {}).get('Lines') or []):
            t = (ln.get('LineText') or '').strip()
            if not t:
                continue
            try:
                h = float(ln.get('MaxHeight') or 0) or max(
                    (float(w.get('Height') or 0) for w in (ln.get('Words') or [])), default=0.0)
            except (TypeError, ValueError):
                h = 0.0
            lines.append({'text': t, 'height': h})
    return {'text': text, 'lines': lines}
