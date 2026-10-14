"""
ISBN -> book metadata lookup, for the barcode scanner feature.

The seller (or their camera) scans the ISBN barcode on the back of a book.
We look that ISBN up against public, no-API-key book databases and return
structured fields (title, author, publisher, edition/year) - much faster
and more reliable than OCR-ing a cover photo, since a barcode decodes to an
exact, unambiguous number instead of noisy scanned text.

Two providers, tried in order:
1. Open Library (https://openlibrary.org/dev/docs/api/books) - free, no key.
2. Google Books (https://developers.google.com/books/docs/v1/using) - free,
   no key required for this read-only volumes-by-isbn lookup.

Either can be missing fields or momentarily unavailable, so we try Open
Library first and fall back to Google Books if it didn't return a usable
title. Network errors are swallowed and reported back as a normal "not
found" result rather than a 500 - a lookup miss is an expected, everyday
outcome (e.g. a self-published or very old book), not a bug.
"""
import re
import logging

import requests

logger = logging.getLogger(__name__)

OPEN_LIBRARY_URL = 'https://openlibrary.org/api/books'
GOOGLE_BOOKS_URL = 'https://www.googleapis.com/books/v1/volumes'
REQUEST_TIMEOUT = 6  # seconds - a scanner UI should fail fast, not hang


def normalize_isbn(raw: str) -> str:
    """Strips whitespace/hyphens. Doesn't validate checksum - just tidies for lookup/storage."""
    return re.sub(r'[^0-9Xx]', '', raw or '').upper()


def _empty_result(isbn):
    return {
        'isbn': isbn, 'title': '', 'author': '', 'publisher': '',
        'edition': '', 'publication_year': '', 'source': '', 'found': False,
    }


def _from_open_library(isbn):
    resp = requests.get(
        OPEN_LIBRARY_URL,
        params={'bibkeys': f'ISBN:{isbn}', 'format': 'json', 'jscmd': 'data'},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get(f'ISBN:{isbn}')
    if not data:
        return None

    authors = ', '.join(a.get('name', '') for a in data.get('authors', []) if a.get('name'))
    publishers = ', '.join(p.get('name', '') for p in data.get('publishers', []) if p.get('name'))
    year_match = re.search(r'\d{4}', data.get('publish_date', '') or '')

    return {
        'isbn': isbn,
        'title': data.get('title', '') or '',
        'author': authors,
        'publisher': publishers,
        'edition': data.get('edition_name', '') or '',
        'publication_year': year_match.group(0) if year_match else '',
        'source': 'Open Library',
        'found': bool(data.get('title')),
    }


def _from_google_books(isbn):
    resp = requests.get(
        GOOGLE_BOOKS_URL, params={'q': f'isbn:{isbn}'}, timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get('items') or []
    if not items:
        return None

    info = items[0].get('volumeInfo', {})
    year_match = re.search(r'\d{4}', info.get('publishedDate', '') or '')

    return {
        'isbn': isbn,
        'title': info.get('title', '') or '',
        'author': ', '.join(info.get('authors', [])),
        'publisher': info.get('publisher', '') or '',
        'edition': '',  # Google Books rarely exposes a clean "edition" field
        'publication_year': year_match.group(0) if year_match else '',
        'source': 'Google Books',
        'found': bool(info.get('title')),
    }


def lookup_isbn(raw_isbn: str) -> dict:
    """Looks up book metadata for a scanned/typed ISBN. Always returns a dict; never raises."""
    isbn = normalize_isbn(raw_isbn)
    if len(isbn) not in (10, 13):
        result = _empty_result(isbn)
        result['error'] = 'That doesn\'t look like a valid ISBN (should be 10 or 13 digits).'
        return result

    for provider in (_from_open_library, _from_google_books):
        try:
            result = provider(isbn)
            if result and result['found']:
                return result
        except requests.RequestException:
            logger.warning('ISBN lookup provider failed for %s', isbn, exc_info=True)
            continue

    result = _empty_result(isbn)
    result['error'] = 'No book found for this ISBN. You can still fill in the details manually.'
    return result
