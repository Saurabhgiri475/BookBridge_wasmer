"""
AI step of the "Scan Book Cover" feature.

Pipeline:  photo -> OCR (all the text on the cover) -> THIS MODULE (an LLM reads
that text and decides which piece is the title, author, publisher, edition,
ISBN and publication year) -> the listing form is pre-filled.

Two LLMs are tried in order, each only if its key is configured:
  1. Gemini (settings.GEMINI_API_KEY)  - text-only call, no image sent.
  2. Groq   (settings.GROQ_API_KEY)    - OpenAI-compatible chat endpoint.
Any failure raises AiParseError; the caller (ocr_utils) then keeps the plain
regex/layout guesses, so an AI outage never breaks scanning.
"""
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_GROQ_MODEL = 'llama-3.3-70b-versatile'
REQUEST_TIMEOUT = 15
MAX_INPUT_CHARS = 4000  # a cover has far less text than this; caps cost if OCR returns junk

FIELDS = ('title', 'author', 'publisher', 'isbn', 'edition', 'publication_year')

SYSTEM_PROMPT = """You read the raw OCR text of a book's cover (and sometimes back cover) and \
sort it into catalogue fields for an Indian used-book marketplace. The OCR text can contain \
mistakes, stray characters, taglines, prices, barcodes, addresses and advertising. Lines may be \
prefixed with "[size NN]" - the relative type size (100 = biggest on the cover); the title is \
usually the biggest type.

Go through EVERY line and decide what it is: title, author, publisher, edition, ISBN, \
publication year, or noise. Reply with ONLY one JSON object, no prose, no markdown fences:

{"title": string, "author": string, "publisher": string, "isbn": string, \
"edition": string, "publication_year": string}

Rules:
- "title": the book's main title, including a volume/part number or subtitle only if it is \
clearly part of the title. Not the tagline ("For JEE Main"), not the author, not the publisher.
- "author": the person(s) who wrote it; several authors joined with ", ". Drop "by", "Dr.", "Prof." \
only if they are clearly just prefixes. Not editors-in-chief of a series unless no author exists.
- "publisher": the publishing house (e.g. "Arihant Publications", "Oxford University Press").
- "isbn": digits only (and a trailing X for ISBN-10), no hyphens or the word ISBN. Must be 10 or 13 \
digits as printed - never invent or complete missing digits.
- "edition": like "3rd Edition" or "Revised Edition".
- "publication_year": a 4-digit publication/copyright/edition year. Never a price, phone number, \
pincode or page count.
- You may fix an obvious OCR typo in a word you can clearly recognise (e.g. "Phvsics" -> "Physics"). \
Do not add words that are not in the text, and never use outside knowledge to fill a field the \
text does not support.
- Use an empty string for any field the text does not contain."""


class AiParseError(Exception):
    """Any problem with the AI step - the caller falls back to the plain heuristics."""


# ---------- helpers ----------

def isbn_is_valid(digits: str) -> bool:
    """Checksum test for ISBN-10 / ISBN-13 (digits only, optional trailing X for ISBN-10)."""
    digits = digits.upper()
    if len(digits) == 13 and digits.isdigit():
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
        return (10 - total % 10) % 10 == int(digits[12])
    if len(digits) == 10 and re.fullmatch(r'\d{9}[\dX]', digits):
        total = sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(digits))
        return total % 11 == 0
    return False


def _extract_json(text: str):
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', (text or '').strip()).strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _clean(fields: dict) -> dict:
    out = {k: re.sub(r'\s+', ' ', str(fields.get(k) or '')).strip() for k in FIELDS}
    isbn = re.sub(r'[^0-9Xx]', '', out['isbn']).upper()
    out['isbn'] = isbn if isbn_is_valid(isbn) else ''
    m = re.search(r'\b(19|20)\d{2}\b', out['publication_year'])
    out['publication_year'] = m.group(0) if m else ''
    for k in ('title', 'author', 'publisher', 'edition'):
        out[k] = out[k][:200]
    return out


def _call_gemini(text: str, api_key: str, model: str) -> dict:
    payload = {
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'contents': [{'parts': [{'text': 'OCR text of the cover:\n\n' + text}]}],
        'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json'},
    }
    try:
        resp = requests.post(
            GEMINI_URL.format(model=model or DEFAULT_GEMINI_MODEL),
            headers={'x-goog-api-key': api_key, 'Content-Type': 'application/json'},
            json=payload, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AiParseError(f'Could not reach Gemini: {exc}') from exc
    if resp.status_code != 200:
        logger.warning('Gemini text parse returned %s: %s', resp.status_code, resp.text[:300])
        raise AiParseError(f'Gemini error (status {resp.status_code}).')
    try:
        parts = resp.json()['candidates'][0]['content']['parts']
        answer = ''.join(p.get('text', '') for p in parts)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AiParseError('Gemini returned no usable answer.') from exc
    fields = _extract_json(answer)
    if fields is None:
        raise AiParseError('Gemini did not return valid JSON.')
    return fields


def _call_groq(text: str, api_key: str, model: str) -> dict:
    payload = {
        'model': model or DEFAULT_GROQ_MODEL,
        'temperature': 0,
        'max_tokens': 400,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': 'OCR text of the cover:\n\n' + text},
        ],
    }
    try:
        resp = requests.post(
            GROQ_URL, json=payload, timeout=REQUEST_TIMEOUT,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        )
    except requests.RequestException as exc:
        raise AiParseError(f'Could not reach Groq: {exc}') from exc
    if resp.status_code != 200:
        logger.warning('Groq cover parse returned %s: %s', resp.status_code, resp.text[:300])
        raise AiParseError(f'Groq error (status {resp.status_code}).')
    try:
        answer = resp.json()['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AiParseError('Groq returned no usable answer.') from exc
    fields = _extract_json(answer)
    if fields is None:
        raise AiParseError('Groq did not return valid JSON.')
    return fields


def format_text_for_ai(raw_text: str, layout_lines=None) -> str:
    """OCR text, one line per row. If the OCR engine gave per-line type heights,
    prefix each line with its relative size so the model can spot the title."""
    lines = [l for l in (layout_lines or []) if l.get('text', '').strip() and l.get('height', 0) > 0]
    if len(lines) >= 2:
        tallest = max(l['height'] for l in lines)
        text = '\n'.join(
            '[size %d] %s' % (round(100 * l['height'] / tallest), re.sub(r'\s+', ' ', l['text']).strip())
            for l in lines)
    else:
        text = '\n'.join(re.sub(r'\s+', ' ', l).strip() for l in (raw_text or '').splitlines() if l.strip())
    return text[:MAX_INPUT_CHARS]


# ---------- public API ----------

def parse_cover_text_with_ai(raw_text: str, layout_lines=None, *, gemini_key='', gemini_model='',
                             groq_key='', groq_model='') -> dict:
    """Returns {'title','author','publisher','isbn','edition','publication_year',
    'engine'} (empty string = not found). Raises AiParseError if no LLM could answer."""
    text = format_text_for_ai(raw_text, layout_lines)
    if not text.strip():
        raise AiParseError('No text to analyse.')

    attempts = []
    if gemini_key:
        attempts.append(('Gemini', lambda: _call_gemini(text, gemini_key, gemini_model)))
    if groq_key:
        attempts.append(('Groq', lambda: _call_groq(text, groq_key, groq_model)))
    if not attempts:
        raise AiParseError('No AI key configured (GEMINI_API_KEY / GROQ_API_KEY).')

    last_error = None
    for label, call in attempts:
        try:
            fields = _clean(call())
        except AiParseError as exc:
            logger.warning('%s cover-text parse failed, trying next AI', label, exc_info=True)
            last_error = exc
            continue
        if any(fields.values()):
            fields['engine'] = label
            return fields
        last_error = AiParseError(f'{label} found nothing in the text.')
    raise last_error
