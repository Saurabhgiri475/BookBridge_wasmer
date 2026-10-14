"""
Real ML price model: a scikit-learn regression model trained on completed
Order sales, upgrading price_recommendation.py's rule-based blend the same
way ml_recommendations.py upgrades books/recommendations.py's plain SQL
"also bought" query - a trained model when there's enough sales history to
make one meaningful, an honest heuristic fallback otherwise.

How it works
------------
Every completed sale (a delivered/accepted/shipped Order for a purchase, not
a rental) is a training example: the book's condition, category, age, and
listed original price are the features, and what it actually sold for
(Order.final_price) is the target. A RandomForestRegressor learns the
mapping from those features to a realistic sale price - trees handle the
mix of categorical (condition, category) and numeric (age, original price)
features without needing manual scaling, and cope fine with the wobbly,
non-linear relationships real resale pricing has (a "like_new" 1-year-old
book isn't just condition-multiplier times age-multiplier in the real
world; a tree can pick up on interactions like that from the data itself).

This needs a batch of real sales to be worth anything, so - exactly like
ml_recommendations.py - it's trained offline by
`python manage.py train_price_model` and persisted to disk with joblib, not
recomputed on every page load. price_recommendation.recommend_price() loads
the saved model at request time, blends its prediction in as one more
signal alongside the existing rule-based factors, and simply doesn't use it
(falling back to the pre-existing rule-based blend alone) whenever:
  - no model has been trained yet,
  - the model file is missing/unreadable for any reason, or
  - a required feature is missing for this particular prediction.

This is the same "real thing when we have enough data, honest heuristic
fallback otherwise" shape as ml_recommendations.py, price_recommendation.py,
and llm_search.py elsewhere in this codebase.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

MODEL_PATH = settings.BASE_DIR / 'ml_models' / 'price_model.joblib'

# Need at least this many completed sales before a regression model means
# anything - below this a random forest is just memorizing noise.
MIN_TRAINING_SAMPLES = 30

COUNTED_STATUSES = ['placed', 'accepted', 'shipped', 'out_for_delivery', 'delivered']

FEATURE_COLUMNS = ['condition', 'category_id', 'age_years', 'original_price']


class NotEnoughData(Exception):
    """Raised by build_model() when sales history is too thin to train a
    meaningful price model yet - the training command reports this plainly
    rather than saving a model that's really just noise."""


def _extract_training_rows():
    """Yields one dict of FEATURE_COLUMNS + 'price' per completed sale that
    has enough information to learn from (condition and a sale price are
    required; category/age/original_price are allowed to be missing - the
    model is trained to cope with that, same as the live prediction path)."""
    from datetime import date

    from orders.models import Order

    orders = (
        Order.objects
        .filter(order_type='purchase', status__in=COUNTED_STATUSES, final_price__isnull=False)
        .select_related('book')
    )
    for order in orders:
        book = order.book
        if book is None or not book.condition:
            continue
        age_years = None
        if book.publication_year:
            age_years = max(0, date.today().year - book.publication_year)
        yield {
            'condition': book.condition,
            'category_id': book.category_id,
            'age_years': age_years,
            'original_price': float(book.original_price) if book.original_price else None,
            'price': float(order.final_price),
        }


def build_model():
    """
    Trains a RandomForestRegressor on completed sales and returns the
    fitted sklearn Pipeline (preprocessing + model bundled together, so
    predict_price() doesn't need to duplicate any encoding logic).
    Raises NotEnoughData if there isn't enough sales history yet.
    """
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    rows = list(_extract_training_rows())
    if len(rows) < MIN_TRAINING_SAMPLES:
        raise NotEnoughData(
            f'Only {len(rows)} completed sale(s) with usable data - need at least '
            f'{MIN_TRAINING_SAMPLES} for a meaningful price model. Keep selling; '
            f'try again once more orders have come in (or run `seed_demo` for local testing).'
        )

    df = pd.DataFrame(rows)
    X = df[FEATURE_COLUMNS]
    y = df['price']

    # category_id is categorical (an ID standing in for a category, not a
    # magnitude) so it's one-hot encoded alongside condition; age_years and
    # original_price are genuinely numeric and passed through as-is. Missing
    # values (a book with no publication_year or original_price) are
    # imputed rather than dropping the row, since those two fields are
    # optional on the listing form and shouldn't disqualify a sale from
    # training just because a seller skipped them.
    preprocessor = ColumnTransformer([
        ('condition', OneHotEncoder(handle_unknown='ignore'), ['condition']),
        ('category', Pipeline([
            ('impute', SimpleImputer(strategy='constant', fill_value=-1)),
            ('encode', OneHotEncoder(handle_unknown='ignore')),
        ]), ['category_id']),
        ('numeric', SimpleImputer(strategy='median'), ['age_years', 'original_price']),
    ])

    model = Pipeline([
        ('preprocess', preprocessor),
        ('regressor', RandomForestRegressor(n_estimators=200, max_depth=8, random_state=42)),
    ])
    model.fit(X, y)
    return model, len(rows)


def save_model(model, sample_count):
    import joblib

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({'model': model, 'sample_count': sample_count}, MODEL_PATH)


def load_model():
    """Returns (model, sample_count), or None if no usable model is on disk.

    Also returns None when the optional ML libraries (joblib / scikit-learn /
    pandas) are not installed - the price engine then simply skips the ML signal."""
    try:
        import joblib
    except ImportError:
        return None

    if not MODEL_PATH.exists():
        return None
    try:
        payload = joblib.load(MODEL_PATH)
        return payload['model'], payload['sample_count']
    except Exception:
        logger.exception('Could not load price model at %s - ignoring it.', MODEL_PATH)
        return None


def predict_price(*, condition, category_id, age_years, original_price):
    """Returns (predicted_price: float, sample_count: int), or None if no
    usable model is on disk. Never raises - a bad/missing model means the
    caller just doesn't get an ML signal this time, same as load_model()."""
    loaded = load_model()
    if loaded is None:
        return None
    model, sample_count = loaded

    try:
        import pandas as pd
    except ImportError:
        return None

    row = pd.DataFrame([{
        'condition': condition,
        'category_id': category_id,
        'age_years': age_years,
        'original_price': original_price,
    }])
    try:
        predicted = float(model.predict(row)[0])
    except Exception:
        logger.exception('Price model prediction failed - ignoring it.')
        return None
    return max(predicted, 0.0), sample_count
