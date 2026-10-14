"""
Duplicate Listing Detection.

Warns a seller when they try to list a book they already have an active
listing for - mirrors price_recommendation.py's pattern: a small,
dependency-free module with one entry point (find_duplicate_listings) that
both the live AJAX check and the add/edit views call, so the rule lives in
exactly one place.

"Duplicate" means this seller already has another *active* listing
(status 'available' or 'reserved' - not sold/rented/removed, since those
aren't really "still listed" anymore) that looks like the same book:
  - same ISBN (exact match, when both listings have one filled in), or
  - same title (case-insensitive), narrowed by author when given.
"""
ACTIVE_STATUSES = ['available', 'reserved', 'under_review']


def find_duplicate_listings(seller, *, title='', isbn='', author='', exclude_pk=None):
    """Active listings by `seller` that look like the same book as the
    title/isbn/author given. `exclude_pk` leaves out the listing being
    edited so editing a book doesn't flag itself as a duplicate of itself."""
    from .models import Book

    title = (title or '').strip()
    isbn = (isbn or '').strip()
    author = (author or '').strip()

    if not title and not isbn:
        return Book.objects.none()

    qs = Book.objects.filter(seller=seller, status__in=ACTIVE_STATUSES)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    if isbn:
        by_isbn = qs.filter(isbn=isbn)
        if by_isbn.exists():
            return by_isbn

    if title:
        by_title = qs.filter(title__iexact=title)
        if author:
            narrowed = by_title.filter(author__iexact=author)
            if narrowed.exists():
                return narrowed
        return by_title

    return qs.none()
