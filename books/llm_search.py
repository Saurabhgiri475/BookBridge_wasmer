"""
LLM-backed "AI Search" query parser - the real-AI upgrade path the README's
"Extending the AI search" section describes.

books/nlp_search.py's parse_query() is rule-based (regex + hardcoded lists) -
genuinely useful, but not a model. This module calls the Groq API (free
tier, OpenAI-compatible) to do the same job with actual language
understanding: it copes with phrasing the regex parser was never taught
(typos, synonyms, a university/branch that isn't in KNOWN_UNIVERSITIES/
KNOWN_BRANCHES, a query in Hinglish), at the cost of needing an API key and
a network round trip.

Uses Groq's OpenAI-compatible chat completions endpoint directly via
`requests` (no extra SDK dependency) - the same choice vision_ocr.py made
for Google Vision, for the same reason: one already-installed library, no
extra dependency to pin.

Wiring: books/views.py tries this first when settings.GROQ_API_KEY is set,
and falls back to nlp_search.parse_query() on any failure (missing key,
network error, malformed response) - never lets an LLM outage take down
search. See books/search_ai.py for that fallback wrapper.
"""
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

GROQ_CHAT_COMPLETIONS_URL = 'https://api.groq.com/openai/v1/chat/completions'
REQUEST_TIMEOUT = 8  # seconds - a search box should fail fast onto the rule-based parser, not hang
MAX_OUTPUT_TOKENS = 300

SYSTEM_PROMPT = """You extract structured filters from a student's book-search query for an \
Indian used-textbook marketplace. Respond with ONLY a single JSON object, no prose, no \
markdown code fences, matching exactly this shape:

{"semester": string or null, "university": string or null, "board": string or null, \
"branch": string or null, "exam": string or null, "keywords": string}

Rules:
- "semester": the numeral only, e.g. "3", or null if none is mentioned.
- "university": the institution's common short name/abbreviation if one is named \
(e.g. "AKTU", "Anna University", "Delhi University"), else null.
- "board": a school board if named (e.g. "CBSE", "ICSE", "State Board"), else null.
- "branch": an engineering/degree branch if named (e.g. "CSE", "Mechanical", "BBA"), else null.
- "exam": a competitive exam if named (e.g. "JEE", "NEET", "UPSC", "GATE"), else null.
- "keywords": the remaining subject/title search terms with the above modifiers, semester \
phrases, and filler words ("I need", "book for") stripped out - what should actually be \
matched against book titles/subjects. Never empty; fall back to the cleaned original query.

Only use these fields. Do not invent a value that isn't actually in the query."""


class LLMSearchError(Exception):
    """Raised on any problem calling or parsing the LLM response - callers
    catch this and fall back to the rule-based parser in nlp_search.py."""


def parse_query_with_llm(raw_query: str, api_key: str, model: str) -> dict:
    """
    Same return shape as nlp_search.parse_query(): a dict with 'raw',
    'semester', 'university', 'board', 'branch', 'exam', 'keywords'.
    Raises LLMSearchError on any failure - the caller decides what to do
    (fall back to the rule-based parser; see books/search_ai.py).
    """
    query = (raw_query or '').strip()
    if not query:
        raise LLMSearchError('Empty query.')
    if not api_key:
        raise LLMSearchError('No Groq API key configured.')

    payload = {
        'model': model,
        'max_tokens': MAX_OUTPUT_TOKENS,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': query},
        ],
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    try:
        resp = requests.post(GROQ_CHAT_COMPLETIONS_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise LLMSearchError(f'Could not reach Groq API: {exc}') from exc

    if resp.status_code != 200:
        logger.warning('Groq API returned %s: %s', resp.status_code, resp.text[:500])
        raise LLMSearchError(f'Groq API error (status {resp.status_code}).')

    try:
        data = resp.json()
        text = data['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMSearchError(f'Unexpected Groq API response shape: {exc}') from exc

    parsed = _extract_json(text)
    if parsed is None:
        raise LLMSearchError(f'Could not parse JSON from model output: {text[:200]!r}')

    keywords = parsed.get('keywords')
    if not isinstance(keywords, str) or not keywords.strip():
        keywords = query  # never hand back an empty search - fall back to the raw query

    return {
        'raw': raw_query,
        'semester': _clean_field(parsed.get('semester')),
        'university': _clean_field(parsed.get('university')),
        'board': _clean_field(parsed.get('board')),
        'branch': _clean_field(parsed.get('branch')),
        'exam': _clean_field(parsed.get('exam')),
        'keywords': keywords.strip(),
    }


def _clean_field(value):
    """The model is instructed to use null for "not present", but treat any
    non-string or blank value the same way rather than trusting it strictly -
    a downstream .filter(x__icontains=None) would crash the search page."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_json(text):
    """Models occasionally wrap JSON in a ```json fence despite instructions
    not to - strip that before parsing, and fail closed (return None) rather
    than guessing at malformed output."""
    text = text.strip()
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
