"""
Google Cloud Vision OCR for the "Scan Book Cover" feature.

Uses Vision's REST API directly (a plain HTTPS POST with an API key) rather
than the google-cloud-vision SDK, so we don't need a service-account JSON
file or an extra heavyweight dependency - just requests, which the project
already uses elsewhere (see isbn_lookup.py).

Setup (optional - the app works without it, see ocr_utils.scan_cover_image):
1. Create/select a project at https://console.cloud.google.com
2. Enable the "Cloud Vision API" for that project.
3. Create an API key: APIs & Services > Credentials > Create Credentials >
   API key. For safety, restrict the key to the Cloud Vision API.
4. Put it in your .env as GOOGLE_VISION_API_KEY=... (see .env.example).
   Vision has a free monthly quota; the project's billing account is only
   charged if you go over it.
"""
import base64
import logging

import requests

logger = logging.getLogger(__name__)

VISION_URL = 'https://vision.googleapis.com/v1/images:annotate'
REQUEST_TIMEOUT = 10  # seconds - a scanner UI should fail fast, not hang
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # matches the upload limit enforced in views.scan_cover


class GoogleVisionError(Exception):
    """Raised on any Vision API failure - callers catch this and fall back to Tesseract."""


def run_google_vision_ocr(image_bytes: bytes, api_key: str) -> str:
    """
    Sends image bytes to Google Cloud Vision's TEXT_DETECTION feature and
    returns the full detected text as one string (matching the shape
    pytesseract.image_to_string() returns, so the same downstream field
    heuristics work unchanged on either engine's output).

    Raises GoogleVisionError on any problem (bad key, quota, network, no
    text found) - never returns a partial/garbage result silently.
    """
    if not api_key:
        raise GoogleVisionError('No Google Vision API key configured.')
    if not image_bytes:
        raise GoogleVisionError('No image data to send.')
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise GoogleVisionError('Image too large for Vision OCR.')

    payload = {
        'requests': [
            {
                'image': {'content': base64.b64encode(image_bytes).decode('ascii')},
                # DOCUMENT_TEXT_DETECTION is Vision's "dense text" mode -
                # tuned for pages/blocks of text rather than single signs,
                # which suits a book cover better than plain TEXT_DETECTION.
                'features': [{'type': 'DOCUMENT_TEXT_DETECTION'}],
            }
        ]
    }

    try:
        resp = requests.post(
            VISION_URL,
            params={'key': api_key},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleVisionError(f'Could not reach Google Vision: {exc}') from exc

    if resp.status_code != 200:
        logger.warning('Google Vision API returned %s: %s', resp.status_code, resp.text[:500])
        raise GoogleVisionError(f'Google Vision API error (status {resp.status_code}).')

    data = resp.json()
    responses = data.get('responses') or []
    if not responses:
        raise GoogleVisionError('Google Vision returned no response.')

    first = responses[0]
    if 'error' in first:
        raise GoogleVisionError(f"Google Vision API error: {first['error'].get('message', 'unknown error')}")

    full_annotation = first.get('fullTextAnnotation')
    if full_annotation and full_annotation.get('text'):
        return full_annotation['text']

    # Fall back to the first text-annotation entry (whole-image text) if
    # fullTextAnnotation wasn't populated for some reason.
    text_annotations = first.get('textAnnotations') or []
    if text_annotations:
        return text_annotations[0].get('description', '')

    raise GoogleVisionError('No text detected in image.')
