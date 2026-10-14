"""
Matching helpers shared between:
- books.views.add_book, which calls these right after a new listing is
  published to decide who to notify (bookmarket/notify.py does the actual
  sending)
- books.views.matching_requests_for_seller, the existing pull-model page

Kept as plain functions (not model methods / signals) so it's obvious from
add_book's view code what triggers a notification, matching this project's
existing convention (see bookmarket/notify.py's module docstring).
"""


def find_matching_saved_searches(book):
    """SavedSearch rows (excluding the seller's own) whose filters match this book."""
    from .models import SavedSearch
    return [
        s for s in SavedSearch.objects.exclude(user=book.seller).select_related('user')
        if s.matches(book)
    ]


def title_overlap(a, b):
    a, b = (a or '').strip().lower(), (b or '').strip().lower()
    return bool(a) and bool(b) and (a in b or b in a)


def find_matching_book_requests(book):
    """Open BookRequests (excluding the seller's own) this new listing satisfies."""
    from .models import BookRequest
    matched = []
    for r in BookRequest.objects.filter(status='open').exclude(buyer=book.seller).select_related('buyer'):
        isbn_hit = bool(r.isbn and book.isbn and r.isbn.strip() == book.isbn.strip())
        if isbn_hit or title_overlap(r.title, book.title):
            matched.append(r)
    return matched
