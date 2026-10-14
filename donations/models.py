"""
Book donation with certificate.

Flow:  donor submits a donation to a recipient organisation (NGO / school /
library) -> staff accept it -> the books are handed over -> staff confirm how
many books actually arrived -> a numbered, verifiable certificate is issued for
THAT verified count (never the count the donor typed in, so a certificate can't
be inflated by over-claiming).

Only an issued, non-revoked certificate counts toward the Impact numbers.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.translation import gettext_lazy as _

MAX_ITEMS_PER_DONATION = 20       # title rows on the form
MAX_BOOKS_PER_DONATION = 500      # total copies in one donation (claimed or verified)

# No 0/O/1/I/L - easy to read out loud or type from a printed certificate.
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


class DonationPartner(models.Model):
    """A recipient organisation that books can be donated to. Managed by staff
    (Django admin) - donors can only pick from the active ones."""
    TYPE_CHOICES = (
        ('ngo', _('NGO / Charity')),
        ('school', _('School')),
        ('library', _('Library')),
        ('other', _('Other')),
    )
    name = models.CharField(max_length=150)
    org_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='ngo')
    city = models.CharField(max_length=100)
    address = models.CharField(max_length=255, blank=True, help_text='Drop-off address shown to donors.')
    description = models.TextField(blank=True, help_text='Who the books go to / what the organisation does.')
    website = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)
    registration_number = models.CharField(
        max_length=60, blank=True, help_text='Optional: 80G / 12A / society registration number.',
    )
    accepts_pickup = models.BooleanField(
        default=False, help_text='Can BookBridge arrange to collect books from the donor for this organisation?',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.city})'


class Donation(models.Model):
    STATUS_CHOICES = (
        ('submitted', _('Submitted - awaiting review')),
        ('accepted', _('Accepted - awaiting handover')),
        ('received', _('Received - certificate issued')),
        ('rejected', _('Rejected')),
        ('cancelled', _('Cancelled')),
    )
    HANDOVER_CHOICES = (
        ('dropoff', _('I will drop the books off')),
        ('pickup', _('Please arrange a pickup from me')),
    )
    donor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='donations')
    partner = models.ForeignKey(DonationPartner, on_delete=models.PROTECT, related_name='donations')
    certificate_name = models.CharField(
        max_length=120,
        help_text='The name printed on the certificate. It is also shown on the public verification page.',
    )
    handover = models.CharField(max_length=10, choices=HANDOVER_CHOICES, default='dropoff')
    pickup_address = models.CharField(max_length=255, blank=True)
    pickup_city = models.CharField(max_length=100, blank=True)
    pickup_pincode = models.CharField(max_length=10, blank=True)
    contact_phone = models.CharField(max_length=15, blank=True)
    note = models.TextField(blank=True, max_length=1000)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='submitted', db_index=True)
    rejection_reason = models.CharField(max_length=300, blank=True)
    received_count = models.PositiveIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(MAX_BOOKS_PER_DONATION * 2)],
        help_text='Books verified as received - this is the number printed on the certificate.',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Donation #{self.pk} by {self.donor} to {self.partner.name}'

    @property
    def claimed_count(self):
        """Total copies the donor said they'd give (sum of the item rows)."""
        return self.items.aggregate(n=Sum('quantity'))['n'] or 0

    @property
    def is_open(self):
        return self.status in ('submitted', 'accepted')

    @property
    def can_cancel(self):
        return self.is_open

    @property
    def active_certificate(self):
        cert = getattr(self, 'certificate', None)
        return cert if cert and not cert.revoked else None


class DonationItem(models.Model):
    CONDITION_CHOICES = (
        ('like_new', _('Like new')),
        ('good', _('Good')),
        ('fair', _('Fair (readable, some wear)')),
    )
    donation = models.ForeignKey(Donation, on_delete=models.CASCADE, related_name='items')
    title = models.CharField(max_length=200)
    author = models.CharField(max_length=150, blank=True)
    isbn = models.CharField(max_length=20, blank=True)
    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES, default='good')
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(MAX_BOOKS_PER_DONATION)],
    )

    def __str__(self):
        return f'{self.quantity} x {self.title}'


def _new_certificate_code():
    return 'BBD-' + get_random_string(10, allowed_chars=CODE_ALPHABET)


class DonationCertificate(models.Model):
    """Issued by staff once receipt is verified. Name / organisation / count are
    snapshotted so later edits to the donation or partner can never silently
    change a certificate that has already been handed out. The random code (not
    the database id) is what the public verification page uses, so certificates
    can't be enumerated."""
    donation = models.OneToOneField(Donation, on_delete=models.CASCADE, related_name='certificate')
    code = models.CharField(max_length=20, unique=True, default=_new_certificate_code, editable=False)
    recipient_name = models.CharField(max_length=120)
    partner_name = models.CharField(max_length=150)
    partner_city = models.CharField(max_length=100, blank=True)
    books_count = models.PositiveIntegerField()
    issued_at = models.DateTimeField(default=timezone.now)
    revoked = models.BooleanField(default=False)
    revoked_reason = models.CharField(max_length=300, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-issued_at']

    def __str__(self):
        return f'{self.code} - {self.recipient_name} ({self.books_count} books)'
