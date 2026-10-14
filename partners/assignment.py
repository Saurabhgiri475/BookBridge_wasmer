"""
Lead routing: offer each new buyback lead to ONE nearby partner at a time.

    new lead -> nearest eligible partner gets it exclusively for LEAD_RESPONSE_MINUTES
             -> no offer / pass / shop paused in that time -> next-nearest partner
             -> after LEAD_MAX_ASSIGNMENT_ROUNDS exclusive rounds (or when nobody
                eligible is left) the lead opens to every partner in range, as before.

"Responding" means making an offer or passing. A partner who lets the window lapse is
remembered in `skipped_partners` and is not given that lead exclusively again (they can
still see and bid on it once it is in the open pool).

Nothing here runs by itself - `sweep_expired()` has to be called regularly:
  * `python manage.py reassign_stale_leads` from cron / a scheduler every few minutes, and
  * it is also called (cheaply) whenever a partner opens their dashboard, so the
    system degrades gracefully if the scheduler is not set up yet.

Scope note: this covers leads nobody has answered. A lead where the seller already accepted
an offer but the partner never turns up is a different problem (the seller chose that
partner) and is intentionally not auto-reassigned here.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import BuybackRequest, ShopkeeperPartner
from .utils import nearby_open_requests

logger = logging.getLogger(__name__)

DEFAULT_RESPONSE_MINUTES = 120
DEFAULT_MAX_ROUNDS = 3


def response_minutes():
    return int(getattr(settings, 'LEAD_RESPONSE_MINUTES', DEFAULT_RESPONSE_MINUTES))


def max_rounds():
    return int(getattr(settings, 'LEAD_MAX_ASSIGNMENT_ROUNDS', DEFAULT_MAX_ROUNDS))


def candidate_partners(buyback):
    """Live partners whose own radius covers this lead, nearest first (same-area
    matches without coordinates last), excluding the seller and anyone who already
    lapsed/passed. Distance logic is shared with the dashboard (utils.nearby_open_requests)."""
    skipped = buyback.skipped_partners.values('pk')
    partners = (
        ShopkeeperPartner.objects.filter(status='approved', is_accepting_requests=True)
        .exclude(user=buyback.seller).exclude(pk__in=skipped).select_related('user')
    )
    single = BuybackRequest.objects.filter(pk=buyback.pk)
    ranked = []
    for partner in partners:
        matches = nearby_open_requests(partner, single, radius_km=partner.default_radius_km)
        if matches:
            distance = matches[0].distance_km
            ranked.append((distance is None, distance or 0.0, partner.pk, partner))
    ranked.sort(key=lambda r: r[:3])
    return [r[3] for r in ranked]


def _notify(fn_name, *args):
    """Notifications must never break routing (email/SMS providers fail)."""
    def run():
        try:
            from bookmarket import notify
            getattr(notify, fn_name)(*args)
        except Exception:
            logger.exception('lead routing notification %s failed', fn_name)
    transaction.on_commit(run)


def assign_next(buyback, now=None):
    """Reserve `buyback` for the next candidate, or open it to everyone. Call inside
    transaction.atomic() with the row locked. Returns the partner, or None if open."""
    now = now or timezone.now()
    candidates = candidate_partners(buyback)
    if candidates and buyback.assignment_round < max_rounds():
        partner = candidates[0]
        buyback.assigned_partner = partner
        buyback.assignment_expires_at = now + timedelta(minutes=response_minutes())
        buyback.assignment_round += 1
        buyback.save(update_fields=['assigned_partner', 'assignment_expires_at', 'assignment_round', 'updated_at'])
        _notify('notify_partner_lead_reserved', partner, buyback, response_minutes())
        return partner

    buyback.assigned_partner = None
    buyback.assignment_expires_at = None
    buyback.save(update_fields=['assigned_partner', 'assignment_expires_at', 'updated_at'])
    for partner in candidates:  # open pool: tell everyone in range who has not already had their turn
        _notify('notify_partner_new_lead', partner, buyback)
    return None


def route_new_lead(buyback):
    with transaction.atomic():
        locked = BuybackRequest.objects.select_for_update().get(pk=buyback.pk)
        assign_next(locked)
        return locked.assigned_partner


def _lapse(buyback, now, reason_fn='notify_partner_lead_lapsed'):
    lapsed = buyback.assigned_partner
    buyback.skipped_partners.add(lapsed)
    assign_next(buyback, now)
    _notify(reason_fn, lapsed, buyback)


def sweep_expired(now=None):
    """Move on every reserved lead whose window ran out, or whose partner has since
    paused / been suspended. Idempotent and safe to run concurrently. Returns how many moved."""
    now = now or timezone.now()
    stale = BuybackRequest.objects.filter(status='pending', assigned_partner__isnull=False).filter(
        Q(assignment_expires_at__lte=now)
        | ~Q(assigned_partner__status='approved')
        | Q(assigned_partner__is_accepting_requests=False)
    ).values_list('pk', flat=True)
    moved = 0
    for pk in list(stale):
        with transaction.atomic():
            lead = (
                BuybackRequest.objects.select_for_update()
                .filter(pk=pk, status='pending', assigned_partner__isnull=False).first()
            )
            if not lead:
                continue  # answered or cancelled while we were looping
            partner = lead.assigned_partner
            still_valid = (
                lead.assignment_expires_at and lead.assignment_expires_at > now
                and partner.status == 'approved' and partner.is_accepting_requests
            )
            if still_valid:
                continue
            _lapse(lead, now)
            moved += 1
    return moved


def pass_lead(buyback_pk, partner):
    """The reserved partner explicitly declines - hand it on right away instead of
    making the seller wait out the window. Returns True if it was theirs to pass."""
    with transaction.atomic():
        lead = (
            BuybackRequest.objects.select_for_update()
            .filter(pk=buyback_pk, status='pending', assigned_partner=partner).first()
        )
        if not lead:
            return False
        lead.skipped_partners.add(partner)
        assign_next(lead)
        return True


def restart_after_rejection(buyback, rejected_partner):
    """The seller declined this partner's offer and the lead went back to 'pending'
    (BuybackRequest.reopen). Start routing afresh, without that partner."""
    with transaction.atomic():
        lead = BuybackRequest.objects.select_for_update().get(pk=buyback.pk)
        lead.skipped_partners.add(rejected_partner)
        lead.assignment_round = 0
        lead.save(update_fields=['assignment_round', 'updated_at'])
        assign_next(lead)


def time_left_label(lead, now=None):
    now = now or timezone.now()
    if not lead.assignment_expires_at:
        return ''
    seconds = int((lead.assignment_expires_at - now).total_seconds())
    if seconds <= 0:
        return 'expiring now'
    minutes = -(-seconds // 60)
    hours, mins = divmod(minutes, 60)
    return f'{hours}h {mins:02d}m' if hours else f'{mins}m'
