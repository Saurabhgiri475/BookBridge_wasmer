"""Business rules for donations (kept out of the views so they're easy to test)."""
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from .models import Donation, DonationCertificate, _new_certificate_code
from django.utils.translation import gettext as _


class DonationStateError(Exception):
    """The requested transition isn't allowed from the donation's current state."""


def accept_donation(donation, staff):
    with transaction.atomic():
        d = Donation.objects.select_for_update().get(pk=donation.pk)
        if d.status != 'submitted':
            raise DonationStateError(_('Only a submitted donation can be accepted.'))
        d.status = 'accepted'
        d.reviewed_by, d.reviewed_at = staff, timezone.now()
        d.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return d


def reject_donation(donation, staff, reason):
    with transaction.atomic():
        d = Donation.objects.select_for_update().get(pk=donation.pk)
        if not d.is_open:
            raise DonationStateError(_('Only an open donation can be rejected.'))
        d.status = 'rejected'
        d.rejection_reason = reason
        d.reviewed_by, d.reviewed_at = staff, timezone.now()
        d.save(update_fields=['status', 'rejection_reason', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return d


def cancel_donation(donation):
    with transaction.atomic():
        d = Donation.objects.select_for_update().get(pk=donation.pk)
        if not d.can_cancel:
            raise DonationStateError(_('This donation can no longer be cancelled.'))
        d.status = 'cancelled'
        d.save(update_fields=['status', 'updated_at'])
    return d


def issue_certificate(donation, staff, received_count):
    """Marks the donation received and issues its certificate for the VERIFIED
    count. Safe against double-clicks: the row is locked and a second call fails."""
    if received_count < 1:
        raise DonationStateError(_('Received count must be at least 1.'))
    with transaction.atomic():
        d = Donation.objects.select_for_update().select_related('partner').get(pk=donation.pk)
        if not d.is_open:
            raise DonationStateError(_('Only an open donation can be marked received.'))
        if DonationCertificate.objects.filter(donation=d).exists():
            raise DonationStateError(_('A certificate was already issued for this donation.'))
        d.status = 'received'
        d.received_count = received_count
        d.reviewed_by, d.reviewed_at = staff, timezone.now()
        d.save(update_fields=['status', 'received_count', 'reviewed_by', 'reviewed_at', 'updated_at'])
        for _attempt in range(5):  # the code is random; retry on the (astronomically rare) collision
            try:
                with transaction.atomic():
                    cert = DonationCertificate.objects.create(
                        donation=d, code=_new_certificate_code(),
                        recipient_name=d.certificate_name, partner_name=d.partner.name,
                        partner_city=d.partner.city, books_count=received_count,
                    )
                break
            except IntegrityError:
                continue
        else:  # pragma: no cover
            raise DonationStateError(_('Could not generate a unique certificate code. Please retry.'))
    return cert


def revoke_certificate(certificate, reason):
    if certificate.revoked:
        raise DonationStateError(_('This certificate is already revoked.'))
    certificate.revoked = True
    certificate.revoked_reason = reason
    certificate.revoked_at = timezone.now()
    certificate.save(update_fields=['revoked', 'revoked_reason', 'revoked_at'])
    return certificate


# --- Impact figures (only issued, non-revoked certificates count) -----------------

def _valid_certs():
    return DonationCertificate.objects.filter(revoked=False)


def books_donated_total():
    return _valid_certs().aggregate(n=Sum('books_count'))['n'] or 0


def books_donated_by(user):
    return _valid_certs().filter(donation__donor=user).aggregate(n=Sum('books_count'))['n'] or 0


def donation_stats():
    agg = _valid_certs().aggregate(books=Sum('books_count'), donations=Count('id'), donors=Count('donation__donor', distinct=True),
                                   orgs=Count('donation__partner', distinct=True))
    return {
        'books_donated': agg['books'] or 0,
        'donations_count': agg['donations'] or 0,
        'donor_count': agg['donors'] or 0,
        'org_count': agg['orgs'] or 0,
    }
