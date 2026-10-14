"""
AI Book Recognition (OCR).

When a seller uploads a photo of a book's cover, we run it through Tesseract
OCR (offline, no API key required) to pull out raw text, then apply
regex/heuristics to guess the title, author, publisher, edition, and ISBN so
the seller doesn't have to type everything by hand.

This is genuinely functional today (no external API key needed), but heuristic
in nature -- results are always shown to the seller as *editable, pre-filled
suggestions*, never auto-submitted, since OCR + heuristics can't be perfectly
reliable on every cover design/font.

To upgrade later: swap `extract_fields()`'s internals for a call to a
vision-capable LLM (e.g. send the image + a prompt asking for structured
JSON back) for much higher accuracy on stylised covers -- the calling code
in views.py doesn't need to change, since it just consumes the returned dict.
"""
import re
import pytesseract
from PIL import Image, ImageOps, ImageFilter


ISBN_RE = re.compile(r'(?:ISBN[-:\s]*)?((?:97[89][-\s]?)?\d(?:[-\s]?\d){8}[-\s]?[\dXx])')
YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')
EDITION_RE = re.compile(r'\b(\d+(?:st|nd|rd|th))\s+Edition\b', re.IGNORECASE)
AUTHOR_HINT_RE = re.compile(r'\bby\s+([A-Z][A-Za-z.\'\-]+(?:\s+[A-Z][A-Za-z.\'\-]+){0,3})', re.IGNORECASE)

# Words that are almost never part of a title/author -- filtered out of guesses
NOISE_WORDS = {
    'edition', 'revised', 'new', 'international', 'ncert', 'cbse', 'icse',
    'publications', 'publication', 'publishers', 'press', 'books', 'book',
}


def preprocess_image(image_file):
    """Improve OCR accuracy: grayscale, autocontrast, upscale if small, sharpen."""
    img = Image.open(image_file).convert('L')
    img = ImageOps.autocontrast(img)
    if img.width < 800:
        ratio = 800 / img.width
        img = img.resize((800, int(img.height * ratio)))
    img = img.filter(ImageFilter.SHARPEN)
    return img


def run_ocr(image_file) -> str:
    img = preprocess_image(image_file)
    return pytesseract.image_to_string(img)


def _clean_lines(raw_text: str):
    lines = []
    for line in raw_text.splitlines():
        line = line.strip(' -_|•·\t')
        if len(line) < 2:
            continue
        # skip lines that are almost entirely non-alphabetic (barcodes/noise)
        letters = sum(c.isalpha() for c in line)
        if letters < max(2, len(line) * 0.4):
            continue
        lines.append(line)
    return lines


def extract_fields(image_file) -> dict:
    """Returns a dict of best-guess fields plus the raw OCR text for transparency."""
    raw_text = run_ocr(image_file)
    lines = _clean_lines(raw_text)

    result = {
        'title': '', 'author': '', 'publisher': '', 'edition': '',
        'isbn': '', 'publication_year': '', 'raw_text': raw_text.strip(),
        'confidence': 'low',
    }
    if not lines:
        return result

    isbn_match = ISBN_RE.search(raw_text)
    if isbn_match:
        result['isbn'] = re.sub(r'[-\s]', '', isbn_match.group(1))

    year_match = YEAR_RE.search(raw_text)
    if year_match:
        result['publication_year'] = year_match.group(0)

    edition_match = EDITION_RE.search(raw_text)
    if edition_match:
        result['edition'] = edition_match.group(1)

    author_match = AUTHOR_HINT_RE.search(raw_text)
    if author_match:
        result['author'] = author_match.group(1).strip()

    # Title heuristic: the longest of the first few cleaned lines that isn't
    # pure noise -- cover designs almost always put the title in large type
    # near the top, which OCR usually (though not always) reads first.
    candidates = lines[:6]
    title_candidates = [
        l for l in candidates
        if l.lower().strip('.,:;') not in NOISE_WORDS and len(l) > 2
    ]
    if title_candidates:
        result['title'] = max(title_candidates, key=len).title()

    # Publisher heuristic: look for a line containing a noise word like
    # "Publications"/"Press" anywhere in the text.
    for line in lines:
        low = line.lower()
        if any(w in low for w in ('publication', 'publisher', 'press')):
            result['publisher'] = line.title()
            break

    filled = sum(bool(result[k]) for k in ('title', 'author', 'isbn', 'edition'))
    result['confidence'] = 'high' if filled >= 3 else ('medium' if filled >= 1 else 'low')

    return result
