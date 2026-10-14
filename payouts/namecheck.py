"""
Does the name a UPI app shows for a UPI ID belong to the same person as the
seller's BookBridge account name?

UPI apps typically show the bank-registered name in capitals ("RAJESH KUMAR
GAUR"), while people sign up as "Rajesh Gaur" or "R K Gaur". So this is a
forgiving comparison - word order, case, titles and initials don't matter - but
it will not match two different people: every word of the shorter name must be
found in the longer one, and at least one of them has to be a full word, not just
an initial.
"""
import re
from difflib import SequenceMatcher

TITLES = {'mr', 'mrs', 'ms', 'miss', 'shri', 'shree', 'sri', 'smt', 'shrimati', 'kumari', 'km', 'dr', 'late', 'sh'}
TYPO_RATIO = 0.85   # "Sharma" vs "Sharmaa"; only applied to words of 5+ letters


def name_tokens(name):
    words = re.sub(r"[^a-z\s]", ' ', (name or '').casefold().replace('.', ' ')).split()
    return [w for w in words if w not in TITLES]


def _token_match(a, b):
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:            # an initial matches a word that starts with it
        return a[0] == b[0]
    return len(a) >= 5 and len(b) >= 5 and SequenceMatcher(None, a, b).ratio() >= TYPO_RATIO


def names_match(account_name, upi_name):
    a, b = name_tokens(account_name), name_tokens(upi_name)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    remaining = list(long_)
    full_word_hit = False
    for tok in short:
        hit = next((c for c in remaining if _token_match(tok, c)), None)
        if hit is None:
            return False
        remaining.remove(hit)
        if len(tok) > 1 and len(hit) > 1:
            full_word_hit = True
    return full_word_hit
