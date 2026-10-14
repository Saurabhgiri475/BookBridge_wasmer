"""
Real ML recommendation model: item-item collaborative filtering trained with
scikit-learn, upgrading books/recommendations.py's SQL-only "also bought"
query - the upgrade path the README's stubbed-features table names directly
("Recommendation engine (collaborative) -> scikit-learn collaborative
filtering").

How it works
------------
Build a sparse buyer x book interaction matrix from completed Order history
(one row per buyer, one column per book, 1 if that buyer bought that book),
then compute book-to-book cosine similarity between columns with
sklearn.metrics.pairwise.cosine_similarity. Two books are "similar" here if
they tend to be bought by the same people - the textbook definition of
item-based collaborative filtering (the same family of technique behind
"customers who bought this also bought" on most real e-commerce sites).

This needs a batch of purchase history to be worth anything, so it's trained
offline by `python manage.py train_recommendations` (mirrors seed_demo/
seed_coupons in structure) and persisted to disk with joblib - not computed
on every page load. books/recommendations.py loads the saved model at
request time and falls back to its original real-time SQL query (which
still works with zero training step, just with a cruder "count the buyer
overlap" notion of similarity) whenever:
  - no model has been trained yet,
  - the trained model doesn't cover this book yet (bought too recently /
    too rarely to have been included in the last training run), or
  - the model file is missing/unreadable for any reason.

This is the same "real thing when we have enough data, honest heuristic
fallback otherwise" shape as price_recommendation.py and llm_search.py in
this codebase.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

MODEL_PATH = settings.BASE_DIR / 'ml_models' / 'item_similarity.joblib'

# Need at least this many distinct buyers with 2+ purchases each (i.e. an
# actual co-purchase signal to learn from) before a trained model means
# anything - below this, cosine similarity is just noise from 1-2 orders.
MIN_BUYERS_WITH_REPEAT_PURCHASES = 5

COUNTED_STATUSES = ['placed', 'accepted', 'shipped', 'out_for_delivery', 'delivered']


class NotEnoughData(Exception):
    """Raised by build_model() when purchase history is too thin to train
    a meaningful model yet - the training command reports this plainly
    rather than saving a model that's really just noise."""


def build_model():
    """
    Reads Order history and returns (book_ids, similarity_matrix):
      - book_ids: list of Book pks, in the order the matrix's rows/columns
        correspond to.
      - similarity_matrix: an NxN numpy array, similarity_matrix[i][j] is
        the cosine similarity between book_ids[i] and book_ids[j].
    Raises NotEnoughData if there isn't enough purchase history yet.
    """
    from collections import defaultdict

    import numpy as np
    from scipy.sparse import csr_matrix
    from sklearn.metrics.pairwise import cosine_similarity

    from orders.models import Order

    rows = (
        Order.objects.filter(order_type='purchase', status__in=COUNTED_STATUSES)
        .values_list('buyer_id', 'book_id').distinct()
    )

    buyer_purchase_counts = defaultdict(int)
    for buyer_id, _ in rows:
        buyer_purchase_counts[buyer_id] += 1
    repeat_buyers = sum(1 for count in buyer_purchase_counts.values() if count >= 2)
    if repeat_buyers < MIN_BUYERS_WITH_REPEAT_PURCHASES:
        raise NotEnoughData(
            f'Only {repeat_buyers} buyer(s) with 2+ purchases - need at least '
            f'{MIN_BUYERS_WITH_REPEAT_PURCHASES} for a meaningful model. Keep selling; '
            f'try again once more orders have come in (or run `seed_demo` for local testing).'
        )

    book_ids = sorted({book_id for _, book_id in rows})
    book_index = {book_id: i for i, book_id in enumerate(book_ids)}
    buyer_ids = sorted(buyer_purchase_counts)
    buyer_index = {buyer_id: i for i, buyer_id in enumerate(buyer_ids)}

    # Sparse buyer x book interaction matrix - dense would be
    # len(buyers) * len(books) floats, almost all zero, for no reason.
    row_idx, col_idx, data = [], [], []
    for buyer_id, book_id in rows:
        row_idx.append(buyer_index[buyer_id])
        col_idx.append(book_index[book_id])
        data.append(1.0)
    interactions = csr_matrix((data, (row_idx, col_idx)), shape=(len(buyer_ids), len(book_ids)))

    # Item-item similarity: cosine_similarity on the transpose compares
    # columns (books) to each other, using each book's column of buyers as
    # its feature vector - two books bought by an overlapping set of people
    # get a high score, regardless of how many total buyers each has.
    similarity_matrix = cosine_similarity(interactions.T)
    np.fill_diagonal(similarity_matrix, 0)  # a book is never "similar to itself" in the output

    return book_ids, similarity_matrix


def save_model(book_ids, similarity_matrix):
    import joblib

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({'book_ids': book_ids, 'similarity_matrix': similarity_matrix}, MODEL_PATH)


def load_model():
    """Returns (book_ids, similarity_matrix), or None if no usable model is on disk.

    Also returns None when the optional ML libraries (joblib / numpy / scikit-learn)
    are not installed - e.g. on Wasmer, where they are left out to keep the build
    small. Callers then fall back to the real-time SQL "also bought" query."""
    try:
        import joblib
    except ImportError:
        return None

    if not MODEL_PATH.exists():
        return None
    try:
        payload = joblib.load(MODEL_PATH)
        return payload['book_ids'], payload['similarity_matrix']
    except Exception:
        logger.exception('Could not load recommendation model at %s - ignoring it.', MODEL_PATH)
        return None


def similar_book_ids(book_id, book_ids, similarity_matrix, limit):
    """Top `limit` book ids most similar to `book_id`, best first. Returns
    [] if `book_id` wasn't in the training data (a listing added, or first
    sold, after the last training run - the caller falls back to the
    real-time SQL query for these)."""
    try:
        index = book_ids.index(book_id)
    except ValueError:
        return []

    scores = similarity_matrix[index]
    # argsort ascending, then reverse and take the top N - cheaper than a
    # full descending sort for what's normally a few hundred/thousand books.
    ranked_indices = scores.argsort()[::-1]
    result = []
    for i in ranked_indices:
        if scores[i] <= 0:
            break  # cosine similarity is never negative here (all-nonnegative interaction matrix)
        result.append(book_ids[i])
        if len(result) >= limit:
            break
    return result
