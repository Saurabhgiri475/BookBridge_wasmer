"""
Book cover scanning: turn a photographed front cover into structured fields
(title, author, publisher, ISBN, edition, year).

Engines are tried in this order; the first one that gives a usable result
wins, and any failure (no key, quota, network, nothing readable) just moves
on to the next:
  1. OCR.space  - free key, settings.OCR_SPACE_API_KEY. See ocrspace_ocr.py.
  2. Gemini     - free key, settings.GEMINI_API_KEY. Reads the cover and
     returns the fields directly. See gemini_ocr.py.
  3. Google Cloud Vision - optional, only if settings.GOOGLE_VISION_API_KEY
     is set. See vision_ocr.py.
  4. pytesseract (Tesseract OCR) - offline, no API key, always the last resort.

Whichever engine produces the raw text, the same regex/keyword heuristics
below first turn it into guessed fields; then (if GEMINI_API_KEY or
GROQ_API_KEY is set) an LLM reads ALL of that text and decides which part is
the title / author / publisher / edition / ISBN / year - see
ai_cover_parser.py. The AI's answer wins, the heuristics fill any blanks, and
if the AI is unavailable the heuristics alone are used. Results are always
*editable, pre-filled suggestions* for the seller to check, never
auto-submitted.
"""
import re
import logging

from PIL import Image
import pytesseract

from django.conf import settings

from .vision_ocr import run_google_vision_ocr, GoogleVisionError
from .ocr_image import OcrEngineError
from .ocrspace_ocr import run_ocr_space_detailed
from .gemini_ocr import run_gemini_ocr, DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from .ai_cover_parser import parse_cover_text_with_ai, AiParseError

logger = logging.getLogger(__name__)

KNOWN_PUBLISHERS = [
    'NCERT', 'Pearson', 'Oxford University Press', 'Oxford', 'Cambridge University Press', 'Cambridge',
    'McGraw Hill', 'McGraw-Hill', 'Tata McGraw Hill', 'Arihant', 'Arihant Publications', 'S. Chand',
    'S Chand', 'Disha Publications', 'Disha', 'MTG', 'MTG Learning Media', 'Made Easy', 'Selina',
    'Selina Publishers', 'Wiley', 'Vikas Publishing', 'Dorling Kindersley', 'DK', 'Penguin',
    'Penguin Books', 'HarperCollins', 'Bloomsbury', 'Rupa Publications', 'Galgotia', 'Khanna Publishers',
    'PHI Learning', 'Laxmi Publications', 'Pragati Prakashan', 'Cengage', 'Cengage Learning',
]

ISBN_RE = re.compile(r'(?:ISBN[-:\s]*)?(97[89][-\s]?\d{1,5}[-\s]?\d{1,7}[-\s]?\d{1,7}[-\s]?\d)', re.IGNORECASE)
EDITION_RE = re.compile(r'(\d+)\s*(?:st|nd|rd|th)\s+edition', re.IGNORECASE)
AUTHOR_LINE_RE = re.compile(r'^\s*by\s+(.+)$', re.IGNORECASE)
YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')


MAX_TITLE_LINES = 3
MAX_TITLE_CHARS = 100


def _looks_like_author(line: str, publisher=None) -> bool:
    """1-4 words, letters only (dots/hyphens/apostrophes allowed), not the publisher."""
    line = line.strip(' .,')
    words = line.split()
    if not (1 <= len(words) <= 4) or len(line) > 40:
        return False
    if any(c.isdigit() for c in line) or ISBN_RE.search(line) or EDITION_RE.search(line):
        return False
    if publisher and publisher.upper() in line.upper():
        return False
    if any(p.upper() in line.upper() for p in KNOWN_PUBLISHERS):
        return False
    return all(re.fullmatch(r"[A-Za-z.\'\-]+", w) for w in words)


def _clean_lines(raw_text: str):
    lines = [l.strip() for l in raw_text.splitlines()]
    lines = [re.sub(r'\s+', ' ', l) for l in lines if l.strip()]
    return lines


def parse_fields_from_text(raw_text: str) -> dict:
    """
    Applies the title/author/publisher/ISBN/edition/year heuristics to any
    block of OCR'd text, regardless of which engine produced it. Returns a
    dict of guessed fields plus the raw text for transparency (no 'source'
    key - callers set that, since it depends on which engine was used).
    """
    raw_text = raw_text or ''
    lines = _clean_lines(raw_text)

    result = {
        'raw_text': raw_text.strip(),
        'title': None,
        'author': None,
        'publisher': None,
        'isbn': None,
        'edition': None,
        'publication_year': None,
        'confidence_note': '',
    }

    full_text_upper = raw_text.upper()

    # ISBN
    isbn_match = ISBN_RE.search(raw_text)
    if isbn_match:
        result['isbn'] = re.sub(r'[-\s]', '', isbn_match.group(1))

    # Edition
    edition_match = EDITION_RE.search(raw_text)
    if edition_match:
        n = edition_match.group(1)
        suffix = 'th'
        if n.endswith('1') and n != '11':
            suffix = 'st'
        elif n.endswith('2') and n != '12':
            suffix = 'nd'
        elif n.endswith('3') and n != '13':
            suffix = 'rd'
        result['edition'] = f"{n}{suffix} Edition"

    # Publication year (take the last 4-digit year found, often near the bottom/publisher block)
    full_years = re.findall(r'\b(19\d{2}|20\d{2})\b', raw_text)
    if full_years:
        result['publication_year'] = int(full_years[-1])

    # Publisher
    for pub in KNOWN_PUBLISHERS:
        if pub.upper() in full_text_upper:
            result['publisher'] = pub
            break

    # Author - look for an explicit "by X" line first
    for line in lines:
        m = AUTHOR_LINE_RE.match(line)
        if m:
            result['author'] = m.group(1).strip(' .')
            break

    # Title - lines that appear *before* the first author/publisher/ISBN/edition
    # line are usually the (possibly multi-line/wrapped) title on a cover, so
    # we join them rather than only keeping the very first line.
    stop_idx = len(lines)
    for i, line in enumerate(lines):
        is_author_line = bool(AUTHOR_LINE_RE.match(line))
        is_publisher_line = result['publisher'] and result['publisher'].upper() in line.upper()
        is_isbn_line = bool(ISBN_RE.search(line))
        is_edition_line = bool(EDITION_RE.search(line))
        if is_author_line or is_publisher_line or is_isbn_line or is_edition_line:
            stop_idx = i
            break

    # A cover's title is a line or three, never the whole page: cap it, so a
    # cover with no "by ..." line can't dump every line into the title field.
    title_idx = []
    for i, line in enumerate(lines[:stop_idx]):
        digit_ratio = sum(c.isdigit() for c in line) / max(len(line), 1)
        if digit_ratio > 0.4 or len(line) < 2:
            continue
        title_idx.append(i)
    title_idx = title_idx[:MAX_TITLE_LINES]

    if title_idx:
        result['title'] = ' '.join(lines[i] for i in title_idx).strip()
    else:
        # fall back to the old heuristic if nothing landed before the stop index
        candidate_lines = []
        for line in lines:
            if result['publisher'] and result['publisher'].upper() in line.upper():
                continue
            if AUTHOR_LINE_RE.match(line):
                continue
            if ISBN_RE.search(line):
                continue
            digit_ratio = sum(c.isdigit() for c in line) / max(len(line), 1)
            if digit_ratio > 0.4:
                continue
            if len(line) < 3:
                continue
            candidate_lines.append(line)
        if candidate_lines:
            top_half = candidate_lines[: max(1, len(candidate_lines) // 2 + 1)]
            result['title'] = max(top_half, key=len)

    # No explicit "by X" line: the first name-like line right after the title is
    # usually the author.
    if not result['author'] and title_idx:
        for line in lines[title_idx[-1] + 1: title_idx[-1] + 4]:
            if _looks_like_author(line, result['publisher']):
                result['author'] = line.strip(' .,')
                break

    if not result['title'] and not result['author'] and not result['isbn']:
        result['confidence_note'] = (
            'Could not confidently read this cover. Try a clearer, well-lit, '
            'straight-on photo, or fill in the details manually.'
        )
    elif not result['title']:
        result['confidence_note'] = 'Could not confidently detect a title - please check/fill it in.'

    return result


def parse_fields_from_layout(raw_text: str, layout_lines: list) -> dict:
    """
    Like parse_fields_from_text, but uses each line's text height (from OCR.space's
    overlay) to pick the title: it is the biggest type on the cover. Falls back
    to the plain-text result when there's no usable layout data.
    """
    result = parse_fields_from_text(raw_text)
    lines = [l for l in (layout_lines or []) if l.get('text') and l.get('height', 0) > 0]
    if len(lines) < 2:
        return result

    def _is_other_field(text):
        return bool(
            ISBN_RE.search(text) or EDITION_RE.search(text) or AUTHOR_LINE_RE.match(text)
            or (result['publisher'] and result['publisher'].upper() in text.upper())
            or sum(c.isdigit() for c in text) / max(len(text), 1) > 0.4
            or len(text.strip()) < 2
        )

    body = [(i, l) for i, l in enumerate(lines) if not _is_other_field(l['text'])]
    if not body:
        return result
    tallest = max(l['height'] for _, l in body)
    # Title = the big-type lines. Keep reading order, and stop at the first gap so a
    # tall tagline further down the cover isn't glued onto the title.
    big = [(i, l) for i, l in body if l['height'] >= 0.7 * tallest]
    chosen = [big[0]]
    for prev, cur in zip(big, big[1:]):
        if cur[0] - prev[0] > 2:
            break
        chosen.append(cur)
    chosen = chosen[:MAX_TITLE_LINES]
    result['title'] = ' '.join(re.sub(r'\s+', ' ', l['text']).strip() for _, l in chosen)

    # The text-only parser may have guessed an author from the wrong line; unless the
    # cover has an explicit "by X" line, redo that guess using the layout.
    if not any(AUTHOR_LINE_RE.match(l['text']) for l in lines):
        result['author'] = None
    if not result['author']:
        last = chosen[-1][0]
        for l in lines[last + 1: last + 4]:
            if _looks_like_author(l['text'], result['publisher']):
                result['author'] = l['text'].strip(' .,')
                break
    return result


def _run_tesseract_ocr(image_file) -> str:
    image = Image.open(image_file)
    if image.mode != 'RGB':
        image = image.convert('RGB')
    return pytesseract.image_to_string(image)


def extract_book_info(image_file) -> dict:
    """
    Tesseract-only OCR + field parsing (offline, no API key). Kept as its
    own function - used directly by scan_cover_image() as the fallback path,
    and by anything that specifically wants the offline engine.
    """
    raw_text = _run_tesseract_ocr(image_file)
    result = parse_fields_from_text(raw_text)
    result['source'] = 'Tesseract (offline)'
    return result


def _has_useful_fields(result: dict) -> bool:
    # A very long "title" means the whole cover got dumped into it - not usable on its own.
    title_ok = bool(result.get('title')) and len(result['title']) <= MAX_TITLE_CHARS
    return title_ok or bool(result.get('author') or result.get('isbn'))


def _result_from_gemini(g: dict) -> dict:
    """Gemini's structured answer wins; the regex parser fills anything it left blank."""
    result = parse_fields_from_text(g.get('raw_text', ''))
    if g.get('title'):
        result['title'] = g['title']
    if g.get('author'):
        result['author'] = g['author']
    if g.get('publisher'):
        result['publisher'] = g['publisher']
    isbn = re.sub(r'[^0-9Xx]', '', g.get('isbn', ''))
    if len(isbn) in (10, 13):
        result['isbn'] = isbn
    if g.get('edition'):
        result['edition'] = g['edition']
    year = re.search(r'\b(19|20)\d{2}\b', g.get('publication_year', ''))
    if year:
        result['publication_year'] = int(year.group(0))
    if not result['raw_text']:
        result['raw_text'] = g.get('raw_text', '')
    # parse_fields_from_text's note was based on the heuristics alone - redo it.
    result['confidence_note'] = '' if _has_useful_fields(result) else (
        'Could not confidently read this cover. Try a clearer, well-lit, '
        'straight-on photo, or fill in the details manually.'
    )
    return result


def _enrich_with_ai(result: dict, layout_lines=None) -> dict:
    """Send the OCR'd text to an LLM and let it fill title/author/publisher/ISBN/
    edition/year. The AI's non-empty answers win over the regex guesses; anything it
    leaves blank keeps the heuristic value. Never raises - on any AI problem the
    heuristic result is returned unchanged."""
    gemini_key = getattr(settings, 'GEMINI_API_KEY', '')
    groq_key = getattr(settings, 'GROQ_API_KEY', '')
    raw_text = result.get('raw_text') or ''
    if not (gemini_key or groq_key) or not raw_text.strip():
        return result
    try:
        ai = parse_cover_text_with_ai(
            raw_text, layout_lines,
            gemini_key=gemini_key,
            gemini_model=getattr(settings, 'GEMINI_MODEL', '') or GEMINI_DEFAULT_MODEL,
            groq_key=groq_key,
            groq_model=getattr(settings, 'GROQ_MODEL', ''),
        )
    except AiParseError:
        logger.info('AI cover-text parsing unavailable, using plain heuristics', exc_info=True)
        return result
    except Exception:  # an AI hiccup must never break scanning
        logger.warning('Unexpected error in AI cover-text parsing', exc_info=True)
        return result

    for key in ('title', 'author', 'publisher', 'isbn', 'edition'):
        if ai.get(key):
            result[key] = ai[key]
    if ai.get('publication_year'):
        result['publication_year'] = int(ai['publication_year'])
    result['ai_engine'] = ai['engine']
    # The heuristic's "couldn't read it" note may no longer apply.
    result['confidence_note'] = '' if _has_useful_fields(result) else result.get('confidence_note', '')
    return result


def _source_label(label: str, result: dict) -> str:
    return f"{label} + {result['ai_engine']} AI" if result.get('ai_engine') else label


def _read_bytes(image_file) -> bytes:
    image_file.seek(0)
    return image_file.read()


def scan_cover_image(image_file) -> dict:
    """
    Main entry point for the "Scan Book Cover" feature.

    Tries OCR.space, then Gemini (reads the image itself), then Google Vision
    (only if configured), then offline Tesseract. Every plain-OCR engine's text
    is then sent to an LLM (Gemini/Groq) that sorts it into the form fields -
    see _enrich_with_ai(). An engine is skipped when it has no key, and
    abandoned when it errors or reads nothing useful (no title, author or
    ISBN). A seller should never see a hard error just because an online
    engine hiccuped. If every engine comes up empty, the best earlier result
    is still returned (with its confidence note) rather than nothing.
    """
    image_bytes = _read_bytes(image_file)
    best = None  # a weak-but-valid result from an online engine, kept as a last resort

    def _try(label, fn):
        nonlocal best
        try:
            result = fn()
        except (OcrEngineError, GoogleVisionError):
            logger.warning('%s OCR failed, trying next engine', label, exc_info=True)
            return None
        result['source'] = _source_label(label, result)
        if _has_useful_fields(result):
            return result
        logger.info('%s read the image but found no title/author/ISBN, trying next engine', label)
        best = best or result
        return None

    engines = []
    if getattr(settings, 'OCR_SPACE_API_KEY', ''):
        def _ocr_space():
            d = run_ocr_space_detailed(image_bytes, settings.OCR_SPACE_API_KEY)
            return _enrich_with_ai(parse_fields_from_layout(d['text'], d['lines']), d['lines'])
        engines.append(('OCR.space', _ocr_space))
    if getattr(settings, 'GEMINI_API_KEY', ''):
        engines.append(('Google Gemini', lambda: _result_from_gemini(run_gemini_ocr(
            image_bytes, settings.GEMINI_API_KEY,
            getattr(settings, 'GEMINI_MODEL', '') or GEMINI_DEFAULT_MODEL))))
    if getattr(settings, 'GOOGLE_VISION_API_KEY', ''):
        engines.append(('Google Cloud Vision', lambda: _enrich_with_ai(parse_fields_from_text(
            run_google_vision_ocr(image_bytes, settings.GOOGLE_VISION_API_KEY)))))

    for label, fn in engines:
        result = _try(label, fn)
        if result:
            return result

    # Last resort: offline Tesseract.
    image_file.seek(0)
    try:
        result = _enrich_with_ai(extract_book_info(image_file))
        result['source'] = _source_label('Tesseract (offline)', result)
        return result
    except Exception:
        if best:
            logger.warning('Tesseract unavailable; returning the weak online result instead', exc_info=True)
            return best
        raise
