"""
Shopkeeper Partner program.

A registered user can apply to become a "BookBridge Partner" - a local
shopkeeper who buys old books directly from sellers (instant cash, no
waiting for a marketplace buyer) and then resells them. In exchange for
routing these direct-sale sellers to them, the partner pays BookBridge a
commission (default 10%, see ShopkeeperPartner.commission_percent) on
every book they buy this way.

Flow:
  1. A user applies via ShopkeeperPartner (shop details + ID/shop proof),
     same "flag for a human, don't auto-trust" spirit as
     accounts.models.User's Aadhaar verification.
  2. Staff approve/reject the application from the Staff Dashboard
     (adminpanel) - see partners.views + adminpanel/views.py.
  3. Once approved, a seller who'd rather sell straight to BookBridge
     (instead of listing on the open marketplace) posts a BuybackRequest.
     It starts unassigned/open to any approved partner in that city.
  4. An approved partner makes an offer (offered_price). The seller
     accepts or rejects it - reject reopens the request for other
     partners rather than killing it outright.
  5. After the partner has physically collected the book and paid the
     seller, they mark the request paid/completed with the actual amount
     handed over - this is what final_price records, and is what the 10%
     commission is calculated from (see PartnerCommission).
  6. At that same step the partner can optionally relist the book for
     resale on BookBridge under their own account - see
     partners.views.mark_paid, which copies it into a new Book (and its
     photos) and links it back here via `listed_book`.

"Open for Offers" requests are matched to partners by distance
(shop_latitude/longitude vs pickup_latitude/longitude, see
partners.utils.nearby_open_requests) when both sides have coordinates on
file, falling back to a same-city/same-postal-area match otherwise.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from books.models import Book, BookImage, Category
from bookmarket.storage import get_private_storage
from django.utils.translation import gettext_lazy as _

DEFAULT_COMMISSION_PERCENT = Decimal('10.00')
MAX_PICKUP_OTP_ATTEMPTS = 5  # wrong guesses allowed before a request's pickup OTP is locked (seller can reissue)
PICKUP_WINDOW_DAYS = 14      # how far ahead a pickup can be scheduled


class ShopkeeperPartner(models.Model):
    STATUS_CHOICES = (
        ('pending', _('Pending Review')),
        ('approved', _('Approved')),
        ('rejected', _('Rejected')),
        ('suspended', _('Suspended')),
    )

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='partner_profile')

    shop_name = models.CharField(max_length=150)
    shop_address = models.CharField(max_length=255)
    shop_city = models.CharField(max_length=100)
    shop_pincode = models.CharField(max_length=10)
    shop_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
        help_text='Optional, but lets nearby buyback requests find you by distance rather than exact city match.',
    )
    shop_longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    contact_phone = models.CharField(max_length=15)
    gstin = models.CharField(
        max_length=15, blank=True,
        help_text='Optional - GST number, if the shop is registered.',
    )

    # --- Proof of genuineness ----------------------------------------
    # Mirrors accounts.models.User's Aadhaar handling: the raw number is
    # never stored, only a hash (for future re-verification) + the last 4
    # digits (for display), per UIDAI guidance. This is the shop OWNER's
    # ID, kept separate from whatever personal Aadhaar the same person may
    # have already verified as a regular seller - a partner application
    # should stand on its own evidence.
    ID_TYPE_CHOICES = (
        ('aadhaar', _('Aadhaar')),
        ('pan', _('PAN')),
    )
    id_type = models.CharField(
        max_length=10, choices=ID_TYPE_CHOICES, default='aadhaar',
        help_text='Which government ID number the owner entered. The photo can be any government ID.',
    )
    owner_aadhaar_hash = models.CharField(max_length=128, blank=True)
    owner_aadhaar_last4 = models.CharField(max_length=4, blank=True)
    # PAN follows the same rule as Aadhaar: only a hash + the last 4 characters are kept.
    owner_pan_hash = models.CharField(max_length=128, blank=True)
    owner_pan_last4 = models.CharField(max_length=4, blank=True)
    owner_id_proof = models.ImageField(
        upload_to='partners/id_proof/', storage=get_private_storage,
        help_text='Clear photo of the owner\u2019s Aadhaar/PAN/other government ID.',
    )
    shop_proof = models.ImageField(
        upload_to='partners/shop_proof/', storage=get_private_storage,
        help_text='Photo of the shop front, shop license, or GST certificate - anything that shows this is a real shop.',
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    rejection_reason = models.TextField(blank=True)
    commission_percent = models.DecimalField(
        max_digits=4, decimal_places=2, default=DEFAULT_COMMISSION_PERCENT,
        help_text='% of the buyback price this partner owes BookBridge on every completed pickup.',
    )
    is_accepting_requests = models.BooleanField(
        default=True,
        help_text='Turn off to stop seeing new buyback requests without deactivating the account entirely.',
    )

    # --- Business settings (Shop Settings page) ------------------------
    default_radius_km = models.PositiveSmallIntegerField(
        default=25, help_text='How far from your shop you want to see buyback leads by default.',
    )
    target_margin_percent = models.DecimalField(
        max_digits=4, decimal_places=1, default=Decimal('25.0'),
        validators=[MinValueValidator(0), MaxValueValidator(90)],
        help_text='Profit you aim to keep on each resale. Used by the Offer Assistant to suggest how much to pay.',
    )

    applied_at = models.DateTimeField(auto_now_add=True)
    # Set whenever an applicant edits details/documents while still pending, so
    # the reviewer knows the file they opened earlier has changed.
    last_edited_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='partner_reviews',
    )

    class Meta:
        ordering = ['-applied_at']

    def __str__(self):
        return f'{self.shop_name} ({self.get_status_display()})'

    @property
    def is_approved(self):
        return self.status == 'approved'

    @property
    def is_live(self):
        """Approved AND currently open to new requests."""
        return self.status == 'approved' and self.is_accepting_requests

    def set_owner_aadhaar_number(self, aadhaar_number):
        self.owner_aadhaar_hash = make_password(aadhaar_number)
        self.owner_aadhaar_last4 = aadhaar_number[-4:]

    # --- reputation (see partners/badges.py) -------------------------------
    def review_stats(self):
        agg = self.reviews.filter(is_hidden=False).aggregate(avg=models.Avg('rating'), n=models.Count('id'))
        avg = round(float(agg['avg']), 1) if agg['avg'] else None
        return {'average': avg, 'count': agg['n']}

    def set_owner_pan_number(self, pan_number):
        self.owner_pan_hash = make_password(pan_number)
        self.owner_pan_last4 = pan_number[-4:]

    @property
    def id_number_hint(self):
        """Masked ID number for the reviewer / applicant, e.g. 'Aadhaar ending 9012'."""
        if self.id_type == 'pan' and self.owner_pan_last4:
            return f'PAN ending {self.owner_pan_last4}'
        if self.owner_aadhaar_last4:
            return f'Aadhaar ending {self.owner_aadhaar_last4}'
        return ''

    def outstanding_commission(self):
        agg = self.commissions.filter(is_settled=False).aggregate(total=models.Sum('amount'))
        return agg['total'] or Decimal('0.00')


class BuybackRequest(models.Model):
    """A seller offering to sell a book straight to BookBridge (via a
    Shopkeeper Partner) rather than listing it for another user to buy."""
    CONDITION_CHOICES = Book.CONDITION_CHOICES
    PICKUP_SLOT_CHOICES = (
        ('morning', _('Morning (9 AM - 12 PM)')),
        ('afternoon', _('Afternoon (12 PM - 4 PM)')),
        ('evening', _('Evening (4 PM - 8 PM)')),
    )
    STATUS_CHOICES = (
        ('pending', _('Open for Offers')),
        ('offer_made', _('Offer Made')),
        ('accepted', _('Offer Accepted - Awaiting Pickup')),
        ('paid', _('Picked Up & Paid')),
        ('cancelled', _('Cancelled by Seller')),
    )

    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='buyback_requests')
    partner = models.ForeignKey(
        ShopkeeperPartner, on_delete=models.SET_NULL, null=True, blank=True, related_name='buyback_requests',
    )

    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='buyback_requests',
    )
    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    isbn = models.CharField(max_length=20, blank=True)
    edition = models.CharField(max_length=50, blank=True)
    publication_year = models.PositiveIntegerField(blank=True, null=True)

    subject = models.CharField(max_length=150, blank=True)
    board = models.CharField(max_length=100, blank=True, help_text='CBSE, ICSE, State Board, etc.')
    university = models.CharField(max_length=150, blank=True)
    branch = models.CharField(max_length=150, blank=True, help_text='e.g. CSE, Mechanical')
    semester = models.CharField(max_length=20, blank=True)
    language = models.CharField(max_length=50, default='English')

    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='good')
    original_price = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)
    expected_price = models.DecimalField(
        max_digits=8, decimal_places=2, blank=True, null=True,
        help_text='What you\u2019re hoping to get - optional, helps partners decide whether to make an offer.',
    )

    pickup_address = models.CharField(max_length=255)
    pickup_city = models.CharField(max_length=100, db_index=True)
    pickup_pincode = models.CharField(max_length=10, blank=True)
    pickup_latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    pickup_longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # --- Lead routing (see partners/assignment.py) ----------------------------
    # A new lead is offered to ONE nearby partner at a time for a limited window.
    # If they don't make an offer (or pass) before `assignment_expires_at`, it moves
    # to the next-nearest partner. When nobody is left (or the round limit is hit)
    # `assigned_partner` stays empty and the lead is open to every partner in range.
    assigned_partner = models.ForeignKey(
        'ShopkeeperPartner', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_leads',
    )
    assignment_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    assignment_round = models.PositiveSmallIntegerField(default=0)
    skipped_partners = models.ManyToManyField(
        'ShopkeeperPartner', blank=True, related_name='skipped_leads',
        help_text='Partners who let this lead lapse or passed on it - not offered it again exclusively.',
    )
    offered_at = models.DateTimeField(null=True, blank=True, help_text='When the current partner made their offer (for response-time badges).')

    offered_price = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    offer_note = models.CharField(max_length=255, blank=True)
    final_price = models.DecimalField(
        max_digits=8, decimal_places=2, blank=True, null=True,
        help_text='The amount actually handed to the seller at pickup.',
    )
    listed_book = models.OneToOneField(
        Book, on_delete=models.SET_NULL, null=True, blank=True, related_name='buyback_origin',
        help_text='The resale listing the partner published for this book, if any - see mark_paid().',
    )

    # --- Pickup scheduling + handover OTP ---------------------------------
    # Either side proposes a date/slot once the offer is accepted and the
    # other confirms. The OTP is minted when the offer is accepted (so every
    # accepted request has one) and shown to the seller; the partner must
    # type it in at handover before the request can be marked paid - the
    # same "code proves the other person was physically there" idea as
    # Shipment.delivery_otp, with the same wrong-guess lockout.
    pickup_date = models.DateField(null=True, blank=True)
    pickup_slot = models.CharField(max_length=10, choices=PICKUP_SLOT_CHOICES, blank=True)
    pickup_proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    pickup_confirmed = models.BooleanField(default=False)
    pickup_otp = models.CharField(max_length=6, blank=True)
    pickup_otp_attempts = models.PositiveSmallIntegerField(default=0)
    pickup_otp_verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Buyback: {self.title} ({self.seller.username}) - {self.get_status_display()}'

    def get_absolute_url(self):
        return reverse('partners:buyback_detail', args=[self.pk])

    @property
    def is_open(self):
        return self.status == 'pending'

    def visible_to(self, partner):
        """A pending lead is visible to everyone in range unless it is currently
        reserved for one specific partner."""
        return self.assigned_partner_id is None or self.assigned_partner_id == partner.pk

    PICKUP_FIELDS = [
        'pickup_date', 'pickup_slot', 'pickup_proposed_by', 'pickup_confirmed',
        'pickup_otp', 'pickup_otp_attempts', 'pickup_otp_verified_at',
    ]

    def reopen(self):
        """Seller rejected the current offer - put it back in the open pool
        for other partners instead of killing the request outright."""
        self.partner = None
        self.offered_price = None
        self.offer_note = ''
        self.offered_at = None
        self.status = 'pending'
        self.clear_pickup()
        self.save(update_fields=['partner', 'offered_price', 'offer_note', 'offered_at', 'status', 'updated_at'] + self.PICKUP_FIELDS)

    # --- pickup helpers -----------------------------------------------------

    def clear_pickup(self):
        """Wipes schedule + OTP (does not save)."""
        self.pickup_date = None
        self.pickup_slot = ''
        self.pickup_proposed_by = None
        self.pickup_confirmed = False
        self.pickup_otp = ''
        self.pickup_otp_attempts = 0
        self.pickup_otp_verified_at = None

    def issue_pickup_otp(self, save=True):
        """Mints a fresh handover OTP and resets the attempt counter."""
        from orders.utils import generate_otp
        self.pickup_otp = generate_otp()
        self.pickup_otp_attempts = 0
        self.pickup_otp_verified_at = None
        if save:
            self.save(update_fields=['pickup_otp', 'pickup_otp_attempts', 'pickup_otp_verified_at', 'updated_at'])
        return self.pickup_otp

    @property
    def pickup_otp_locked(self):
        return self.pickup_otp_attempts >= MAX_PICKUP_OTP_ATTEMPTS

    @property
    def has_pickup_schedule(self):
        return bool(self.pickup_date and self.pickup_slot)

    @property
    def pickup_summary(self):
        if not self.has_pickup_schedule:
            return ''
        return f'{self.pickup_date:%a, %d %b %Y} - {self.get_pickup_slot_display()}'

    def check_pickup_otp(self, submitted):
        """True if `submitted` matches. Wrong guesses are counted and, after
        MAX_PICKUP_OTP_ATTEMPTS, rejected outright until the seller reissues
        the code - so this isn't a free brute-force oracle for a 6-digit code."""
        import secrets
        if not self.pickup_otp or self.pickup_otp_locked:
            return False
        if secrets.compare_digest(self.pickup_otp, (submitted or '').strip()):
            self.pickup_otp_verified_at = timezone.now()
            self.save(update_fields=['pickup_otp_verified_at', 'updated_at'])
            return True
        self.pickup_otp_attempts += 1
        self.save(update_fields=['pickup_otp_attempts', 'updated_at'])
        return False


class BuybackRequestImage(models.Model):
    buyback_request = models.ForeignKey(BuybackRequest, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='buyback_requests/', storage=get_private_storage)
    angle = models.CharField(max_length=10, choices=BookImage.ANGLE_CHOICES, default='front')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Photo for {self.buyback_request.title}'


class PartnerCommission(models.Model):
    """The 10% (or whatever ShopkeeperPartner.commission_percent is set to)
    a partner owes BookBridge for a completed buyback. Settling it debits
    the partner's own wallet.balance (wallet.services.debit) - kept as its
    own ledger row, rather than folding straight into WalletTransaction,
    so "how much does this partner currently owe" is a single query rather
    than something reconstructed from transaction descriptions."""
    partner = models.ForeignKey(ShopkeeperPartner, on_delete=models.CASCADE, related_name='commissions')
    buyback_request = models.OneToOneField(BuybackRequest, on_delete=models.CASCADE, related_name='commission')
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    is_settled = models.BooleanField(default=False)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        state = 'settled' if self.is_settled else 'due'
        return f'\u20b9{self.amount} commission from {self.partner.shop_name} ({state})'

    @staticmethod
    def compute_amount(final_price, commission_percent):
        return (Decimal(final_price) * Decimal(commission_percent) / Decimal('100')).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )


class PartnerReview(models.Model):
    """A seller's rating of the partner who bought their book. One per completed
    buyback, written only by that buyback's seller - so ratings can't be farmed
    by strangers or by the partner themselves. Staff can hide a review (abuse,
    extortion, wrong shop) without deleting the evidence."""
    partner = models.ForeignKey(ShopkeeperPartner, on_delete=models.CASCADE, related_name='reviews')
    buyback_request = models.OneToOneField(BuybackRequest, on_delete=models.CASCADE, related_name='partner_review')
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='partner_reviews_written')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.CharField(max_length=500, blank=True)
    is_hidden = models.BooleanField(default=False, help_text='Hidden reviews are excluded from ratings and badges.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.rating}/5 for {self.partner.shop_name}'
