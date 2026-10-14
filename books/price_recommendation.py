"""
AI Price Recommendation engine.

Rule-based at its core (no external API/ML dependency needed, works fully
offline) - mirrors the pattern used by nlp_search.parse_query() and
ocr_utils in this app: a genuinely functional heuristic today, with a clean
single entry point (recommend_price) that a real trained model could later
drop into without any caller (views.py, the sell-form widget) needing to
change. That upgrade has now happened: see books/ml_price_model.py.

Blends up to six signals into a Market Price + a Recommended Price range:
  1. Condition       - resale % of MRP typical for the seller's condition
  2. Age             - extra depreciation the older a book is
  3. Edition         - discount if a newer edition of the same book is known
  4. Demand          - wishlists / open buy-requests / competing listings
  5. Previous sales  - actual completed Order history for this/similar books
     (when we have real sales data it's weighted as the strongest signal)
  6. ML price model  - a RandomForestRegressor (books/ml_price_model.py)
     trained offline on completed sales, when one has been trained and
     there's enough history for it to mean anything (see
     `python manage.py train_price_model`). Blended in the same way
     previous-sales data is; simply skipped when no trained model exists
     yet, so this file works identically to before with zero setup.
"""
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Avg, Count, Q

CONDITION_MULTIPLIERS = {
    'like_new': Decimal('0.85'),
    'excellent': Decimal('0.72'),
    'good': Decimal('0.60'),
    'fair': Decimal('0.48'),
    'old': Decimal('0.35'),
}

DEFAULT_BASE_PRICE = Decimal('300')

# Recommended range spread around the computed market price - tuned so a
# ₹450 market price gives roughly a ₹410-470 recommended band.
LOW_FACTOR = Decimal('0.91')
HIGH_FACTOR = Decimal('1.045')


def _money(value):
    return Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _extract_edition_number(edition_text):
    if not edition_text:
        return None
    m = re.search(r'(\d+)', edition_text)
    return int(m.group(1)) if m else None


def _find_similar_books(qs_model, title, isbn, category_id, subject, exclude_pk=None):
    qs = qs_model.objects.filter(status__in=['available', 'sold', 'rented'])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    if isbn:
        by_isbn = qs.filter(isbn=isbn)
        if by_isbn.exists():
            return by_isbn

    title = (title or '').strip()
    if title:
        by_title = qs.filter(title__iexact=title)
        if by_title.exists():
            return by_title
        first_word = title.split()[0] if title.split() else ''
        if first_word and len(first_word) > 2:
            by_title = qs.filter(title__icontains=first_word)
            if by_title.exists():
                return by_title

    if category_id:
        by_cat = qs.filter(category_id=category_id)
        if subject:
            narrowed = by_cat.filter(subject__icontains=subject)
            if narrowed.exists():
                return narrowed
        if by_cat.exists():
            return by_cat

    return qs.none()


def recommend_price(*, title, author='', isbn='', edition='', condition='good',
                     publication_year=None, original_price=None, category_id=None,
                     subject='', exclude_pk=None):
    from books.models import Book, Wishlist, BookRequest
    from orders.models import Order

    factors = []
    similar = _find_similar_books(Book, title, isbn, category_id, subject, exclude_pk)
    similar_ids = list(similar.values_list('pk', flat=True))

    # ---- 1. Condition -> base reference price --------------------------
    condition_mult = CONDITION_MULTIPLIERS.get(condition, CONDITION_MULTIPLIERS['good'])
    condition_label = dict(Book.CONDITION_CHOICES).get(condition, condition)

    if original_price:
        base_price = Decimal(original_price) * condition_mult
        factors.append({
            'label': 'Condition',
            'detail': f"{condition_label} - typically resells for ~{int(condition_mult * 100)}% of MRP",
        })
    else:
        avg_similar = similar.exclude(price__isnull=True).aggregate(a=Avg('price'))['a']
        if avg_similar:
            # Other listings' price already bakes in *their* condition
            # discount, so treat it as an already-discounted reference
            # rather than an MRP (don't apply condition_mult twice).
            base_price = Decimal(avg_similar)
        else:
            base_price = DEFAULT_BASE_PRICE
        factors.append({
            'label': 'Condition',
            'detail': f"{condition_label} condition factored in",
        })

    # ---- 2. Age -----------------------------------------------------------
    age_factor = Decimal('1.0')
    age_years = None
    if publication_year:
        try:
            age_years = max(0, date.today().year - int(publication_year))
        except (TypeError, ValueError):
            age_years = 0
        if age_years > 0:
            reduction = min(Decimal('0.15'), Decimal('0.015') * age_years)
            age_factor = Decimal('1.0') - reduction
            factors.append({
                'label': 'Age',
                'detail': f"~{age_years} yr(s) since publication (-{int(reduction * 100)}%)",
            })
        else:
            factors.append({'label': 'Age', 'detail': 'Recently published'})
    else:
        factors.append({'label': 'Age', 'detail': 'Publication year not provided'})

    # ---- 3. Edition ---------------------------------------------------
    edition_factor = Decimal('1.0')
    seller_edition_num = _extract_edition_number(edition)
    if seller_edition_num and similar_ids:
        editions = [
            _extract_edition_number(e) for e in
            similar.exclude(edition='').values_list('edition', flat=True)
        ]
        editions = [e for e in editions if e]
        if editions:
            latest_edition = max(editions)
            gap = latest_edition - seller_edition_num
            if gap > 0:
                reduction = min(Decimal('0.20'), Decimal('0.05') * gap)
                edition_factor = Decimal('1.0') - reduction
                factors.append({
                    'label': 'Edition',
                    'detail': f"Edition {seller_edition_num} vs latest known edition {latest_edition} "
                              f"(-{int(reduction * 100)}%)",
                })
            else:
                factors.append({'label': 'Edition', 'detail': f"Edition {seller_edition_num} is current/latest known"})
        else:
            factors.append({'label': 'Edition', 'detail': edition})
    else:
        factors.append({'label': 'Edition', 'detail': edition or 'Not specified'})

    # ---- 4. Demand ------------------------------------------------------
    if similar_ids:
        wishlist_count = Wishlist.objects.filter(book_id__in=similar_ids).count()
        views_total = sum(similar.values_list('views', flat=True))
        active_listing_count = similar.filter(status='available').count()
    else:
        wishlist_count = 0
        views_total = 0
        active_listing_count = 0

    open_requests = BookRequest.objects.filter(status='open')
    first_word = title.split()[0] if title and title.split() else None
    request_count = open_requests.filter(title__icontains=first_word).count() if first_word else 0

    demand_score = (request_count * 3) + (wishlist_count * 2) + (views_total * 0.05) - (active_listing_count * 1.5)
    if demand_score >= 6:
        demand_factor, demand_label = Decimal('1.12'), 'High demand'
    elif demand_score >= 2:
        demand_factor, demand_label = Decimal('1.05'), 'Above-average demand'
    elif demand_score <= -4:
        demand_factor, demand_label = Decimal('0.92'), 'Low demand'
    else:
        demand_factor, demand_label = Decimal('1.0'), 'Average demand'

    factors.append({
        'label': 'Demand',
        'detail': f"{demand_label} - {wishlist_count} wishlist(s), {request_count} open buy request(s), "
                  f"{active_listing_count} competing listing(s)",
    })

    # ---- 5. Previous sales -----------------------------------------------
    sales_avg = None
    sales_count = 0
    if similar_ids:
        agg = Order.objects.filter(
            book_id__in=similar_ids, order_type='purchase',
            status__in=['delivered', 'accepted', 'shipped', 'out_for_delivery', 'placed'],
        ).aggregate(avg=Avg('final_price'), n=Count('id'))
        sales_avg = agg['avg']
        sales_count = agg['n'] or 0

    if sales_avg:
        factors.append({
            'label': 'Previous Sales',
            'detail': f"{sales_count} past sale(s) of this/similar book averaged ~\u20b9{_money(sales_avg)}",
        })
    else:
        factors.append({'label': 'Previous Sales', 'detail': 'No completed sales data yet for this book'})

    # ---- Blend ----------------------------------------------------------
    rule_based_price = base_price * age_factor * edition_factor * demand_factor
    if sales_avg and sales_count >= 1:
        weight_sales = Decimal('0.6') if sales_count >= 3 else Decimal('0.45')
        market_price = (Decimal(sales_avg) * weight_sales) + (rule_based_price * (Decimal('1.0') - weight_sales))
    else:
        market_price = rule_based_price

    # ---- 6. ML price model (optional) ------------------------------------
    # A trained RandomForestRegressor (books/ml_price_model.py), when one
    # exists, is blended in as one more signal - same shape as the sales_avg
    # blend above. No model on disk yet, or a prediction failure for this
    # particular input, just means this signal is skipped: the rule-based +
    # sales blend above already stands on its own.
    from books.ml_price_model import predict_price
    ml_result = predict_price(
        condition=condition,
        category_id=category_id,
        age_years=age_years,
        original_price=float(original_price) if original_price else None,
    )
    if ml_result:
        ml_price, ml_sample_count = ml_result
        weight_ml = Decimal('0.55') if ml_sample_count >= 100 else Decimal('0.4')
        market_price = (Decimal(str(ml_price)) * weight_ml) + (market_price * (Decimal('1.0') - weight_ml))
        factors.append({
            'label': 'ML Price Model',
            'detail': f"Regression model trained on {ml_sample_count} completed sale(s) predicts ~\u20b9{_money(ml_price)}",
        })
    else:
        ml_sample_count = 0

    market_price = _money(max(market_price, Decimal('50')))
    recommended_low = _money(market_price * LOW_FACTOR)
    recommended_high = _money(market_price * HIGH_FACTOR)

    confidence = 'low'
    if ml_sample_count >= 100 or sales_count >= 3 or len(similar_ids) >= 5:
        confidence = 'high'
    elif ml_sample_count >= 1 or sales_count >= 1 or similar_ids:
        confidence = 'medium'

    return {
        'market_price': market_price,
        'recommended_low': recommended_low,
        'recommended_high': recommended_high,
        'factors': factors,
        'confidence': confidence,
    }
