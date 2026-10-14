"""
Smart Recommendations: Amazon-style "Students who bought X also bought Y".

also_bought() below tries a real trained ML model first - item-item
collaborative filtering via scikit-learn (books/ml_recommendations.py,
trained offline with `python manage.py train_recommendations`) - and falls
back to a real-time SQL query over Order history when the trained model
isn't available or doesn't cover this book yet (not trained at all, or the
book was listed/first sold after the last training run). Either way the
signal is genuine purchase-history collaborative filtering, not a
popularity or category heuristic; brand-new listings with no sales history
naturally return [] from both paths - book_detail() in views.py fills the
gap with same-subject/category cross-sells so the section still shows
something useful from day one.
"""
import logging

from django.db.models import Count

from .ml_recommendations import load_model, similar_book_ids

logger = logging.getLogger(__name__)

# Orders in these statuses represent a real completed-or-in-progress
# purchase - cancelled/returned orders don't count as "bought".
COUNTED_STATUSES = ['placed', 'accepted', 'shipped', 'out_for_delivery', 'delivered']


def also_bought(book, limit=6):
    """Books bought by the same people who bought `book`, restricted to
    currently available listings (no point recommending something out of
    stock). Returns [] if this book has no purchase history anywhere."""
    ml_result = _also_bought_ml(book, limit)
    if ml_result is not None:
        return ml_result
    return _also_bought_sql(book, limit)


def _also_bought_ml(book, limit):
    """The trained scikit-learn path. Returns None (not []) when the model
    isn't usable for this book at all, so the caller knows to fall back to
    the SQL path rather than treating "the model says nothing" the same as
    "there's a trained answer and it's empty"."""
    from .models import Book

    model = load_model()
    if model is None:
        return None
    book_ids, similarity_matrix = model

    ranked_ids = similar_book_ids(book.pk, book_ids, similarity_matrix, limit=limit * 3)
    if not ranked_ids:
        return None

    available = {b.pk: b for b in Book.objects.filter(pk__in=ranked_ids, status='available')}
    result = [available[pk] for pk in ranked_ids if pk in available][:limit]
    return result or None


def _also_bought_sql(book, limit):
    """Original real-time query: rank by how many distinct buyers overlap
    between `book` and every other book. Needs no training step, so this is
    what covers a book the ML model hasn't seen yet (listed or first sold
    after the last `train_recommendations` run) - cruder than the trained
    model's cosine similarity (it weighs raw overlap count, not how that
    compares to each book's total buyer base), but never stale."""
    from .models import Book
    from orders.models import Order

    buyer_ids = list(
        Order.objects.filter(book=book, order_type='purchase', status__in=COUNTED_STATUSES)
        .values_list('buyer_id', flat=True).distinct()
    )
    if not buyer_ids:
        return []

    ranked = (
        Order.objects.filter(buyer_id__in=buyer_ids, order_type='purchase', status__in=COUNTED_STATUSES)
        .exclude(book=book)
        .values('book_id')
        .annotate(buyers=Count('buyer_id', distinct=True))
        .order_by('-buyers')[:limit * 3]
    )
    ranked_ids = [row['book_id'] for row in ranked]
    if not ranked_ids:
        return []

    available = {b.pk: b for b in Book.objects.filter(pk__in=ranked_ids, status='available')}
    return [available[pk] for pk in ranked_ids if pk in available][:limit]
