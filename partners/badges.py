"""
Partner reputation: ratings (from sellers, see models.PartnerReview) and badges.

Badges are *computed* from real activity every time they are shown, never stored, so
they can't drift out of date or be granted by hand by mistake - a partner whose rating
falls below a threshold simply stops showing the badge. Hidden reviews are excluded.

Thresholds are deliberately conservative and need a minimum number of data points so one
lucky or unlucky transaction can't make or break a badge.
"""
from datetime import timedelta

from django.db.models import F
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _

QUICK_RESPONSE_MAX_MINUTES = 60   # median time from lead posted to offer made
QUICK_RESPONSE_MIN_OFFERS = 5

BADGES = [
    {'code': 'first_pickup', 'name': _('First Pickup'), 'icon': 'bi-box-seam', 'css': 'bg-secondary',
     'description': _('Completed a first buyback')},
    {'code': 'active_buyer', 'name': _('Active Buyer'), 'icon': 'bi-bag-check', 'css': 'bg-info text-dark',
     'description': _('Completed 10 buybacks')},
    {'code': 'pro_partner', 'name': _('Pro Partner'), 'icon': 'bi-award', 'css': 'bg-primary',
     'description': _('Completed 25 buybacks')},
    {'code': 'quick_responder', 'name': _('Quick Responder'), 'icon': 'bi-lightning-charge', 'css': 'bg-warning text-dark',
     'description': format_lazy(_('Median offer time under {minutes} min (over {offers}+ offers)'), minutes=QUICK_RESPONSE_MAX_MINUTES, offers=QUICK_RESPONSE_MIN_OFFERS)},
    {'code': 'trusted', 'name': _('Trusted Partner'), 'icon': 'bi-shield-check', 'css': 'bg-success',
     'description': _('Rated 4.5+ by at least 5 sellers')},
    {'code': 'top_rated', 'name': _('Top Rated'), 'icon': 'bi-star-fill', 'css': 'bg-success',
     'description': _('Rated 4.8+ by at least 15 sellers')},
]


def _median_minutes(partner):
    from .models import BuybackRequest
    rows = BuybackRequest.objects.filter(partner=partner, offered_at__isnull=False).values_list('created_at', 'offered_at')
    minutes = sorted(max(0.0, (o - c).total_seconds() / 60) for c, o in rows)
    if len(minutes) < QUICK_RESPONSE_MIN_OFFERS:
        return None
    mid = len(minutes) // 2
    return minutes[mid] if len(minutes) % 2 else (minutes[mid - 1] + minutes[mid]) / 2


def compute_stats(partner):
    from .models import BuybackRequest
    reviews = partner.review_stats()
    return {
        'completed': BuybackRequest.objects.filter(partner=partner, status='paid').count(),
        'rating': reviews['average'],
        'review_count': reviews['count'],
        'median_response_minutes': _median_minutes(partner),
    }


def badges_for_stats(stats):
    """Pure function (easy to test): stats dict -> list of badge dicts earned."""
    earned = []
    completed, rating, n = stats['completed'], stats['rating'], stats['review_count']
    median = stats['median_response_minutes']
    for badge in BADGES:
        code = badge['code']
        ok = (
            (code == 'first_pickup' and completed >= 1)
            or (code == 'active_buyer' and completed >= 10)
            or (code == 'pro_partner' and completed >= 25)
            or (code == 'quick_responder' and median is not None and median <= QUICK_RESPONSE_MAX_MINUTES)
            or (code == 'trusted' and n >= 5 and rating is not None and rating >= 4.5)
            or (code == 'top_rated' and n >= 15 and rating is not None and rating >= 4.8)
        )
        if ok:
            earned.append(badge)
    return earned


def next_milestones(stats):
    """Short, concrete hints for the partner's own dashboard."""
    hints = []
    c = stats['completed']
    for target, name in ((1, 'First Pickup'), (10, 'Active Buyer'), (25, 'Pro Partner')):
        if c < target:
            hints.append(f'{target - c} more completed buyback{"s" if target - c != 1 else ""} for "{name}"')
            break
    n, r = stats['review_count'], stats['rating']
    if n < 5:
        hints.append(f'{5 - n} more seller review{"s" if 5 - n != 1 else ""} to be eligible for "Trusted Partner"')
    elif r is not None and r < 4.5:
        hints.append(f'Raise your average rating from {r} to 4.5 for "Trusted Partner"')
    return hints


def reputation(partner):
    stats = compute_stats(partner)
    return {'stats': stats, 'badges': badges_for_stats(stats), 'hints': next_milestones(stats)}
