"""
Lightweight rule-based 'AI search' parser.

This is NOT a call to an external LLM (no API key needed), but it mimics the
feature described in the spec: a user can type a natural sentence like
"I need DBMS book for B.Tech 3rd semester AKTU" and the app extracts
structured filters (semester, university, subject) from it using keyword
and regex matching. This keeps the feature genuinely functional offline.

To upgrade this later to true AI/NLP: swap parse_query() internals for a
call to an LLM or a spaCy/sentence-transformers pipeline - the calling code
in views.py doesn't need to change since it just consumes the returned dict.
"""
import re

SEMESTER_WORDS = {
    '1st': '1', 'first': '1', '2nd': '2', 'second': '2', '3rd': '3', 'third': '3',
    '4th': '4', 'fourth': '4', '5th': '5', 'fifth': '5', '6th': '6', 'sixth': '6',
    '7th': '7', 'seventh': '7', '8th': '8', 'eighth': '8',
}

KNOWN_UNIVERSITIES = ['AKTU', 'DU', 'IPU', 'MAKAUT', 'VTU', 'ANNA UNIVERSITY', 'PU', 'JNTU']
KNOWN_BOARDS = ['CBSE', 'ICSE', 'STATE BOARD']
KNOWN_BRANCHES = ['CSE', 'IT', 'ECE', 'EEE', 'MECHANICAL', 'CIVIL', 'MBA', 'BBA', 'BCA', 'MCA']
KNOWN_EXAMS = ['JEE', 'NEET', 'UPSC', 'SSC', 'GATE', 'CAT', 'BANKING']


def parse_query(raw_query: str) -> dict:
    text = raw_query.strip()
    upper = text.upper()
    result = {'raw': raw_query, 'semester': None, 'university': None, 'board': None,
              'branch': None, 'exam': None, 'keywords': text}

    # semester detection: "3rd semester", "sem 3", "semester 3"
    m = re.search(r'(\d)(?:st|nd|rd|th)?\s*sem', text, re.IGNORECASE)
    if m:
        result['semester'] = m.group(1)
    else:
        for word, num in SEMESTER_WORDS.items():
            if re.search(rf'\b{word}\b', text, re.IGNORECASE):
                result['semester'] = num
                break

    for uni in KNOWN_UNIVERSITIES:
        if uni in upper:
            result['university'] = uni
            break

    for board in KNOWN_BOARDS:
        if board in upper:
            result['board'] = board
            break

    for branch in KNOWN_BRANCHES:
        if re.search(rf'\b{branch}\b', upper):
            result['branch'] = branch
            break

    for exam in KNOWN_EXAMS:
        if re.search(rf'\b{exam}\b', upper):
            result['exam'] = exam
            break

    # strip the recognized modifiers out to get a cleaner keyword/subject string
    cleaned = text
    for token in [result['university'], result['board'], result['branch'], result['exam']]:
        if token:
            cleaned = re.sub(re.escape(token), '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\d(?:st|nd|rd|th)?\s*sem(?:ester)?', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bi\s+need\b|\bi\s+want\b|\bfor\b|\bbook\b', '', cleaned, flags=re.IGNORECASE)
    result['keywords'] = re.sub(r'\s+', ' ', cleaned).strip()

    return result
