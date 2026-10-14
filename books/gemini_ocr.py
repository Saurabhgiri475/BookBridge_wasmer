"""
Google Gemini vision for the "Scan Book Cover" feature (second engine).

Free key (no credit card): https://aistudio.google.com/apikey. Free-tier
limits are set by Google and can change, so treat this as a best-effort
fallback - any failure (quota, network, bad key, safety block) just moves on
to the next engine.

Unlike the two plain-OCR engines, Gemini *understands* the cover, so we ask
it for the structured fields directly (as JSON) plus a transcription. The
caller (ocr_utils) still uses the regex heuristics to fill any gaps.
"""
import base64
import json
import logging
import re

import requests

from .ocr_image import OcrEngineError, prepare_image

logger = logging.getLogger(__name__)

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
REQUEST_TIMEOUT = 25
DEFAULT_MODEL = 'gemini-2.5-flash'

PROMPT = (
    'This is a photo of a book cover. Read the text on it and reply with ONLY a JSON '
    'object with these keys: "title", "author", "publisher", "isbn", "edition", '
    '"publication_year", "raw_text". Use an empty string for anything you cannot see '
    'clearly - never guess or invent. "isbn" is digits only (no hyphens). "edition" is '
    'like "3rd Edition". "publication_year" is a 4-digit year. "raw_text" is every line '
    'of text you can read, one per line, top to bottom.'
)


class GeminiOcrError(OcrEngineError):
    """Any Gemini failure - the caller falls through to the next engine."""


def run_gemini_ocr(image_bytes: bytes, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Returns a dict with title/author/publisher/isbn/edition/publication_year/raw_text."""
    if not api_key:
        raise GeminiOcrError('No Gemini API key configured.')
    if not image_bytes:
        raise GeminiOcrError('No image data to send.')

    data, mime = prepare_image(image_bytes, max_bytes=3 * 1024 * 1024)

    payload = {
        'contents': [{
            'parts': [
                {'inlineData': {'mimeType': mime, 'data': base64.b64encode(data).decode('ascii')}},
                {'text': PROMPT},
            ],
        }],
        'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json'},
    }

    try:
        resp = requests.post(
            GEMINI_URL.format(model=model or DEFAULT_MODEL),
            headers={'x-goog-api-key': api_key, 'Content-Type': 'application/json'},  # header, so the key never lands in URL logs
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GeminiOcrError(f'Could not reach Gemini: {exc}') from exc

    if resp.status_code != 200:
        logger.warning('Gemini returned %s: %s', resp.status_code, resp.text[:300])
        raise GeminiOcrError(f'Gemini error (status {resp.status_code}).')

    try:
        candidates = resp.json().get('candidates') or []
        parts = candidates[0]['content']['parts']
        text = ''.join(p.get('text', '') for p in parts).strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise GeminiOcrError('Gemini returned no usable answer.') from exc

    # Models occasionally wrap JSON in ``` fences even in JSON mode.
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()
    try:
        fields = json.loads(text)
    except ValueError as exc:
        raise GeminiOcrError('Gemini did not return valid JSON.') from exc
    if not isinstance(fields, dict):
        raise GeminiOcrError('Gemini returned an unexpected shape.')

    cleaned = {k: str(fields.get(k) or '').strip() for k in (
        'title', 'author', 'publisher', 'isbn', 'edition', 'publication_year', 'raw_text')}
    if not any(cleaned.values()):
        raise GeminiOcrError('Gemini found no text in the image.')
    return cleaned
