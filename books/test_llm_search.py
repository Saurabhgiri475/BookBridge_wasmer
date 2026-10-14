"""
Tests for books/llm_search.py and books/search_ai.py - the real Groq API
call behind "AI Search", and its fallback to the offline regex parser
in books/nlp_search.py.

No real API key or network access is used: requests.post is mocked
throughout, matching how the codebase already tests other outward-facing
integrations by isolating them behind a single function (see
RazorpayConfigError handling in orders/tests.py).
"""
import json
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from .llm_search import LLMSearchError, parse_query_with_llm
from .search_ai import parse_search_query


def _mock_response(status_code=200, content_text=None, body=None):
    resp = Mock()
    resp.status_code = status_code
    if body is not None:
        resp.json.return_value = body
    elif content_text is not None:
        resp.json.return_value = {'choices': [{'message': {'content': content_text}}]}
    resp.text = json.dumps(body) if body is not None else (content_text or '')
    return resp


class ParseQueryWithLlmTests(TestCase):
    def test_no_api_key_raises(self):
        with self.assertRaises(LLMSearchError):
            parse_query_with_llm('DBMS book for 3rd sem AKTU', api_key='', model='llama-3.3-70b-versatile')

    def test_empty_query_raises(self):
        with self.assertRaises(LLMSearchError):
            parse_query_with_llm('   ', api_key='gsk-test', model='llama-3.3-70b-versatile')

    @patch('books.llm_search.requests.post')
    def test_well_formed_response_is_parsed(self, mock_post):
        mock_post.return_value = _mock_response(content_text=json.dumps({
            'semester': '3', 'university': 'AKTU', 'board': None,
            'branch': 'CSE', 'exam': None, 'keywords': 'DBMS',
        }))

        result = parse_query_with_llm(
            'DBMS book for 3rd sem AKTU CSE', api_key='gsk-test', model='llama-3.3-70b-versatile',
        )

        self.assertEqual(result['semester'], '3')
        self.assertEqual(result['university'], 'AKTU')
        self.assertIsNone(result['board'])
        self.assertEqual(result['branch'], 'CSE')
        self.assertEqual(result['keywords'], 'DBMS')
        self.assertEqual(result['raw'], 'DBMS book for 3rd sem AKTU CSE')

    @patch('books.llm_search.requests.post')
    def test_strips_markdown_code_fence(self, mock_post):
        fenced = '```json\n{"semester": null, "university": null, "board": null, ' \
                 '"branch": null, "exam": null, "keywords": "organic chemistry"}\n```'
        mock_post.return_value = _mock_response(content_text=fenced)

        result = parse_query_with_llm('organic chemistry', api_key='gsk-test', model='m')

        self.assertEqual(result['keywords'], 'organic chemistry')

    @patch('books.llm_search.requests.post')
    def test_blank_keywords_falls_back_to_raw_query(self, mock_post):
        mock_post.return_value = _mock_response(content_text=json.dumps({
            'semester': None, 'university': None, 'board': None,
            'branch': None, 'exam': None, 'keywords': '   ',
        }))

        result = parse_query_with_llm('some odd query', api_key='gsk-test', model='m')

        self.assertEqual(result['keywords'], 'some odd query')

    @patch('books.llm_search.requests.post')
    def test_non_200_raises(self, mock_post):
        mock_post.return_value = _mock_response(status_code=429)

        with self.assertRaises(LLMSearchError):
            parse_query_with_llm('anything', api_key='gsk-test', model='m')

    @patch('books.llm_search.requests.post')
    def test_malformed_json_raises(self, mock_post):
        mock_post.return_value = _mock_response(content_text='not json at all')

        with self.assertRaises(LLMSearchError):
            parse_query_with_llm('anything', api_key='gsk-test', model='m')

    @patch('books.llm_search.requests.post')
    def test_network_error_raises(self, mock_post):
        import requests
        mock_post.side_effect = requests.ConnectionError('boom')

        with self.assertRaises(LLMSearchError):
            parse_query_with_llm('anything', api_key='gsk-test', model='m')


class ParseSearchQueryFallbackTests(TestCase):
    """search_ai.parse_search_query() is the one thing views.py calls - it
    must never let a missing key or a flaky API call break the search page."""

    @override_settings(GROQ_API_KEY='')
    def test_no_key_configured_uses_rule_based_parser(self):
        result = parse_search_query('DBMS book for 3rd sem AKTU')

        self.assertEqual(result['semester'], '3')
        self.assertEqual(result['university'], 'AKTU')

    @override_settings(GROQ_API_KEY='gsk-test', GROQ_MODEL='m')
    @patch('books.search_ai.parse_query_with_llm')
    def test_key_configured_uses_llm_result(self, mock_llm):
        mock_llm.return_value = {
            'raw': 'q', 'semester': '5', 'university': None,
            'board': None, 'branch': None, 'exam': None, 'keywords': 'thermodynamics',
        }

        result = parse_search_query('q')

        self.assertEqual(result['semester'], '5')
        self.assertEqual(result['keywords'], 'thermodynamics')
        mock_llm.assert_called_once_with('q', 'gsk-test', 'm')

    @override_settings(GROQ_API_KEY='gsk-test', GROQ_MODEL='m')
    @patch('books.search_ai.parse_query_with_llm')
    def test_llm_failure_falls_back_to_rule_based_parser(self, mock_llm):
        mock_llm.side_effect = LLMSearchError('boom')

        result = parse_search_query('DBMS book for 3rd sem AKTU')

        self.assertEqual(result['semester'], '3')
        self.assertEqual(result['university'], 'AKTU')
