"""
Real Shiprocket integration (https://apidocs.shiprocket.in/).

Shiprocket is a courier aggregator that talks to Delhivery, Ekart,
Xpressbees, DTDC, Shadowfax and others behind one API/dashboard, and
picks (or lets you pick) whichever courier serves a given pincode. This
module is a thin wrapper around their plain REST API using `requests` -
no SDK needed.

Setup required in your Shiprocket dashboard before this works:
1. An account at https://app.shiprocket.in (they have a free tier / trial).
2. SHIPROCKET_EMAIL / SHIPROCKET_PASSWORD env vars - the same login you use
   for the dashboard (Shiprocket's public API auths with account
   credentials, not a separate API key, as of this writing).
3. At least one "Pickup Location" configured under Settings > Pickup
   Locations, and its nickname set as SHIPROCKET_PICKUP_LOCATION.

Marketplace simplification, stated plainly: Shiprocket's adhoc order API
ships FROM one of your registered pickup locations, not from an arbitrary
address per seller. For a real multi-seller marketplace you'd either (a)
run a central warehouse buyers' books get forwarded to, or (b) use
Shiprocket's multi-pickup-location support and register each active
seller's address as its own named pickup location, then pass that
seller's nickname per order. This module supports either - pass
pickup_location explicitly, or it falls back to SHIPROCKET_PICKUP_LOCATION.
"""
import json
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.core.cache import cache

BASE_URL = 'https://apiv2.shiprocket.in/v1/external'
_TOKEN_CACHE_KEY = 'shiprocket_auth_token'


class ShiprocketConfigError(Exception):
    """Raised when SHIPROCKET_EMAIL / SHIPROCKET_PASSWORD aren't configured."""


class ShiprocketAPIError(Exception):
    """Raised when Shiprocket's API returns an error response."""


def _get_token() -> str:
    token = cache.get(_TOKEN_CACHE_KEY)
    if token:
        return token

    if not (settings.SHIPROCKET_EMAIL and settings.SHIPROCKET_PASSWORD):
        raise ShiprocketConfigError(
            'SHIPROCKET_EMAIL / SHIPROCKET_PASSWORD are not set. '
            'Add them to your environment (see .env.example) to create real shipments.'
        )

    resp = requests.post(f'{BASE_URL}/auth/login', json={
        'email': settings.SHIPROCKET_EMAIL,
        'password': settings.SHIPROCKET_PASSWORD,
    }, timeout=15)
    resp.raise_for_status()
    token = resp.json()['token']
    # Tokens are valid ~10 days; cache for 9 to be safe.
    cache.set(_TOKEN_CACHE_KEY, token, timeout=int(timedelta(days=9).total_seconds()))
    return token


def _headers():
    return {'Authorization': f'Bearer {_get_token()}', 'Content-Type': 'application/json'}


def _request(method, path, **kwargs):
    resp = requests.request(method, f'{BASE_URL}{path}', headers=_headers(), timeout=20, **kwargs)
    if resp.status_code == 401:
        # Token expired/invalid server-side even though our cache thought it
        # was fresh - drop it and retry once.
        cache.delete(_TOKEN_CACHE_KEY)
        resp = requests.request(method, f'{BASE_URL}{path}', headers=_headers(), timeout=20, **kwargs)
    if not resp.ok:
        raise ShiprocketAPIError(f'{method} {path} -> {resp.status_code}: {resp.text[:500]}')
    return resp.json()


def create_shipment_order(order, pickup_location=None):
    """
    Creates an adhoc order in Shiprocket for a marketplace Order and returns
    (shiprocket_order_id, shipment_id). Does NOT assign a courier/AWB yet -
    call assign_awb() next.
    """
    book = order.book
    seller = book.seller
    buyer = order.buyer

    payload = {
        'order_id': f'BB-{str(order.order_id)[:8]}',
        'order_date': order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else datetime.now().strftime('%Y-%m-%d %H:%M'),
        'pickup_location': pickup_location or settings.SHIPROCKET_PICKUP_LOCATION,
        'billing_customer_name': buyer.get_full_name() or buyer.username,
        'billing_last_name': '',
        'billing_address': order.delivery_address,
        'billing_city': order.delivery_city,
        'billing_pincode': order.delivery_pincode,
        'billing_state': order.delivery_city,  # spec's Order model doesn't collect state separately
        'billing_country': 'India',
        'billing_email': buyer.email or 'buyer@example.com',
        'billing_phone': buyer.phone or '9999999999',
        'shipping_is_billing': True,
        'order_items': [{
            'name': book.title[:120],
            'sku': f'BOOK-{book.pk}',
            'units': 1,
            'selling_price': str(order.final_price),
        }],
        'payment_method': 'COD' if order.payment_method == 'cod' else 'Prepaid',
        'sub_total': str(order.final_price),
        'length': settings.DEFAULT_PACKAGE_LENGTH_CM,
        'breadth': settings.DEFAULT_PACKAGE_BREADTH_CM,
        'height': settings.DEFAULT_PACKAGE_HEIGHT_CM,
        'weight': settings.DEFAULT_PACKAGE_WEIGHT_KG,
    }
    data = _request('POST', '/orders/create/adhoc', data=json.dumps(payload))
    return str(data.get('order_id')), str(data.get('shipment_id'))


def assign_awb(shipment_id, courier_id=None):
    """
    Assigns a courier and generates a real AWB (tracking) number for a
    shipment. If courier_id is omitted, Shiprocket auto-picks the
    best/cheapest serviceable courier for that route. Returns
    (awb_code, courier_name).
    """
    payload = {'shipment_id': shipment_id}
    if courier_id:
        payload['courier_id'] = courier_id
    data = _request('POST', '/courier/assign/awb', data=json.dumps(payload))
    response = data.get('response', {}).get('data', {})
    return response.get('awb_code', ''), response.get('courier_name', '')


def request_pickup(shipment_id):
    """Schedules a pickup with the assigned courier for the given shipment."""
    return _request('POST', '/courier/generate/pickup', data=json.dumps({'shipment_id': [shipment_id]}))


def cancel_shipment(shiprocket_order_id):
    return _request('POST', '/orders/cancel', data=json.dumps({'ids': [shiprocket_order_id]}))


# Shiprocket's free-text courier status strings, normalized to this app's
# Shipment.STATUS_CHOICES. Anything not listed here is left unmapped (we
# keep the raw string in tracking_history either way).
_STATUS_MAP = {
    'new': 'pickup_pending',
    'pickup scheduled': 'pickup_pending',
    'pickup generated': 'pickup_pending',
    'picked up': 'picked_up',
    'shipped': 'in_transit',
    'in transit': 'in_transit',
    'reached destination hub': 'in_transit',
    'out for delivery': 'out_for_delivery',
    'delivered': 'delivered',
    'cancelled': 'cancelled',
    'undelivered': 'failed',
    'delivery failed': 'failed',
    'rto initiated': 'failed',
}


def normalize_status(raw_status: str):
    return _STATUS_MAP.get((raw_status or '').strip().lower())


def track_by_awb(awb_code):
    """
    Fetches live tracking activity for an AWB. Returns
    (normalized_status_or_None, events) where events is a list of
    {date, status, activity, location} dicts, newest first, straight from
    Shiprocket/the courier's scan data.
    """
    data = _request('GET', f'/courier/track/awb/{awb_code}')
    track_data = (data.get('tracking_data') or {})
    shipment_track = track_data.get('shipment_track_activities') or []
    events = [{
        'date': ev.get('date'),
        'status': ev.get('status'),
        'activity': ev.get('activity'),
        'location': ev.get('location'),
    } for ev in shipment_track]

    current_status_raw = track_data.get('shipment_status') or (events[0]['status'] if events else None)
    return normalize_status(current_status_raw), events
