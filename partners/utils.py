"""
Matching open BuybackRequests to nearby ShopkeeperPartners.

Pure-Python haversine distance rather than a DB-side (PostGIS-style)
query - the request volume this is built for is small enough that
filtering in Python is simpler than adding a geo extension, and it works
identically on the sqlite dev DB and Postgres in production. If this ever
needs to scale to a lot of concurrent open requests, the query in
nearby_open_requests() below is the place to swap in a real geo index.
"""
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371
DEFAULT_MATCH_RADIUS_KM = 25


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (radians(float(v)) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def same_area(partner, buyback):
    """Fallback used whenever either side is missing coordinates: same
    city (case-insensitive), or the first 3 digits of the pincode match
    (same postal/delivery area)."""
    if buyback.pickup_city.strip().lower() == partner.shop_city.strip().lower():
        return True
    if buyback.pickup_pincode and partner.shop_pincode:
        return buyback.pickup_pincode[:3] == partner.shop_pincode[:3]
    return False


def nearby_open_requests(partner, queryset, radius_km=DEFAULT_MATCH_RADIUS_KM):
    """queryset: BuybackRequest queryset already filtered to status='pending'
    (and anything else the caller wants, e.g. excluding the partner's own
    postings). Returns a list (not a queryset) with `.distance_km` set on
    each match - a real number when both sides have coordinates, None when
    the match came from the same-area fallback - sorted nearest-first,
    area-matches last."""
    has_coords = partner.shop_latitude is not None and partner.shop_longitude is not None
    matches = []
    for buyback in queryset:
        if has_coords and buyback.pickup_latitude is not None and buyback.pickup_longitude is not None:
            distance = haversine_km(
                partner.shop_latitude, partner.shop_longitude,
                buyback.pickup_latitude, buyback.pickup_longitude,
            )
            if distance <= radius_km:
                buyback.distance_km = round(distance, 1)
                matches.append(buyback)
        elif same_area(partner, buyback):
            buyback.distance_km = None
            matches.append(buyback)
    matches.sort(key=lambda b: (b.distance_km is None, b.distance_km if b.distance_km is not None else 0))
    return matches


# --- Instant buyback quote -------------------------------------------------

def _round5(value):
    """Round to the nearest \u20b95 (min \u20b91) - partner offers are quoted in round figures."""
    from decimal import Decimal
    return max(Decimal('1'), (Decimal(value) / 5).quantize(Decimal('1')) * 5)


def estimate_buyback_quote(*, title, author='', isbn='', edition='', condition='good',
                           publication_year=None, original_price=None, category_id=None,
                           subject='', quantity=1, city='', expected_price=None):
    """What would a partner typically PAY for this book?

    recommend_price() gives the RESALE price. A partner has to resell at that
    price after paying BookBridge a commission on what they paid and still keep
    their margin, so (same formula as the partner-side Offer Assistant):

        offer = resale * (1 - margin%) / (1 + commission%)

    margin/commission are the averages across partners currently open to leads
    (falling back to the model defaults when there are none yet), so the quote
    tracks the real partner network. It is an estimate - partners set their own
    offers on each request.
    """
    from decimal import Decimal
    from django.db.models import Avg
    from books.price_recommendation import recommend_price
    from .models import DEFAULT_COMMISSION_PERCENT, ShopkeeperPartner

    result = recommend_price(
        title=title, author=author, isbn=isbn, edition=edition, condition=condition,
        publication_year=publication_year, original_price=original_price,
        category_id=category_id, subject=subject,
    )
    resale_low, resale_high = Decimal(result['recommended_low']), Decimal(result['recommended_high'])
    resale_mid = (resale_low + resale_high) / 2

    live = ShopkeeperPartner.objects.filter(status='approved', is_accepting_requests=True)
    avgs = live.aggregate(margin=Avg('target_margin_percent'), commission=Avg('commission_percent'))
    margin = Decimal(avgs['margin'] if avgs['margin'] is not None else '25') / 100
    commission = Decimal(avgs['commission'] if avgs['commission'] is not None else DEFAULT_COMMISSION_PERCENT) / 100

    mid = _round5(resale_mid * (1 - margin) / (1 + commission))
    low = _round5(mid * Decimal('0.85'))
    high = max(_round5(mid * Decimal('1.10')), mid)
    if low > mid:
        low = mid

    quantity = max(1, int(quantity or 1))
    city = (city or '').strip()
    partners_in_city = live.filter(shop_city__iexact=city).count() if city else 0

    note = ''
    if expected_price is not None:
        ask = Decimal(expected_price)
        if ask > high:
            note = (f'Your ask (\u20b9{ask.quantize(Decimal("1"))}) is above what partners typically pay for this book - '
                    f'expect lower offers or a bit of negotiation.')
        elif ask < low:
            note = (f'Your ask (\u20b9{ask.quantize(Decimal("1"))}) is below the typical offer - '
                    f'you could ask for more.')
        else:
            note = (f'Your ask (\u20b9{ask.quantize(Decimal("1"))}) is within the typical range - '
                    f'you should get offers quickly.')

    return {
        'market_price': Decimal(result['market_price']),
        'resale_low': resale_low, 'resale_high': resale_high,
        'offer_low': low, 'offer_mid': mid, 'offer_high': high,
        'quantity': quantity,
        'total_low': low * quantity, 'total_mid': mid * quantity, 'total_high': high * quantity,
        'partners_in_city': partners_in_city, 'partners_total': live.count(),
        'city': city, 'confidence': result['confidence'], 'note': note,
        'factors': result['factors'],
    }
