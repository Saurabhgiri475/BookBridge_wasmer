"""
Single entry point "AI Search" should call: parse_search_query().

Tries the real LLM parser (books/llm_search.py) when a Groq API key is
configured, and falls back to the offline rule-based parser
(books/nlp_search.py) on any failure - missing key, network error, bad
response - so a flaky API call or an unset key never breaks the search page.
This mirrors the exact fallback shape ocr_utils.py already uses for Google
Vision vs. Tesseract in this codebase.
"""
import logging

from django.conf import settings

from .llm_search import LLMSearchError, parse_query_with_llm
from .nlp_search import parse_query

logger = logging.getLogger(__name__)


def parse_search_query(raw_query: str) -> dict:
    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if api_key:
        try:
            return parse_query_with_llm(raw_query, api_key, settings.GROQ_MODEL)
        except LLMSearchError as exc:
            logger.warning('LLM search parse failed, falling back to rule-based parser: %s', exc)
    return parse_query(raw_query)
