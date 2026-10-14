import json
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models import Avg
from django.urls import reverse
from django.utils import timezone

from bookmarket.storage import get_private_storage
from .file_checks import validate_digital_file_content
from django.utils.translation import gettext_lazy as _

# Rental pricing: what % of the buy price a renter pays for each duration
# option, seller opts a physical listing into rentals via Book.is_rentable -
# there's no separate seller-set rent price, it's always derived from
# `price` so a rental stays proportionate if the seller edits the buy price.
# Digital uploads are served straight back to paying buyers, so keep the set
# of things a seller can put there narrow and bounded rather than accepting
# any file of any size.
MIN_PRICE = Decimal('1.00')  # lowest price a listing (or an offer) may have, in rupees
DIGITAL_FILE_EXTENSIONS = ['pdf', 'doc', 'docx', 'zip', 'png', 'jpg', 'jpeg']
DIGITAL_FILE_MAX_BYTES = 25 * 1024 * 1024  # 25 MB


def validate_digital_file_size(uploaded):
    if uploaded.size > DIGITAL_FILE_MAX_BYTES:
        limit_mb = DIGITAL_FILE_MAX_BYTES // (1024 * 1024)
        raise ValidationError(
            f'That file is {uploaded.size / (1024 * 1024):.1f} MB - the limit is {limit_mb} MB. '
            f'Try compressing the PDF or splitting it up.'
        )


RENTAL_DURATION_PERCENTAGES = {
    7: Decimal('15'),
    15: Decimal('22'),
    30: Decimal('30'),
}


class Category(models.Model):
    GROUP_CHOICES = (
        ('academic', _('Academic (School Boards)')),
        ('competitive', _('Competitive Exams')),
        ('college', _('College / University')),
        ('others', _('Others')),
    )
    name = models.CharField(max_length=100, unique=True)
    group = models.CharField(max_length=20, choices=GROUP_CHOICES, default='others')

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['group', 'name']

    def __str__(self):
        return self.name


class Book(models.Model):
    CONDITION_CHOICES = (
        ('like_new', _('Like New')),
        ('excellent', _('Excellent')),
        ('good', _('Good')),
        ('fair', _('Fair')),
        ('old', _('Old')),
    )
    STATUS_CHOICES = (
        ('available', _('Available')),
        ('reserved', _('Reserved (offer accepted)')),
        ('sold', _('Sold')),
        ('rented', _('Rented Out')),
        ('under_review', _('Under Review (flagged by Fraud Detection)')),
        ('removed', _('Removed')),
    )
    LISTING_TYPE_CHOICES = (
        ('physical', _('Physical Book')),
        ('digital', _('Digital Book / Material')),
    )
    DIGITAL_TYPE_CHOICES = (
        ('pdf_notes', _('PDF Notes')),
        ('handwritten_notes', _('Handwritten Notes')),
        ('question_paper', _('Question Papers')),
        ('lab_manual', _('Lab Manuals')),
    )

    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='books')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='books')

    listing_type = models.CharField(max_length=10, choices=LISTING_TYPE_CHOICES, default='physical')
    digital_type = models.CharField(
        max_length=20, choices=DIGITAL_TYPE_CHOICES, blank=True,
        help_text='Only for Digital Books: PDF Notes, Handwritten Notes, Question Papers, or Lab Manuals.',
    )
    digital_file = models.FileField(
        upload_to='digital_books/%Y/%m/', blank=True, null=True, storage=get_private_storage,
        validators=[
            FileExtensionValidator(allowed_extensions=DIGITAL_FILE_EXTENSIONS),
            validate_digital_file_size,
            validate_digital_file_content,
        ],
        help_text='The PDF/scanned file buyers receive by email + download link after payment. '
                  'Max 25 MB; PDF, Word, ZIP or images.',
    )

    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    isbn = models.CharField(max_length=20, blank=True, db_index=True)
    edition = models.CharField(max_length=50, blank=True)
    publication_year = models.PositiveIntegerField(blank=True, null=True)

    subject = models.CharField(max_length=150, blank=True)
    board = models.CharField(max_length=100, blank=True, help_text='CBSE, ICSE, State Board, etc.')
    university = models.CharField(max_length=150, blank=True)
    branch = models.CharField(max_length=150, blank=True, help_text='e.g. CSE, Mechanical')
    semester = models.CharField(max_length=20, blank=True)
    language = models.CharField(max_length=50, default='English')

    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='good')
    # Server-side money rules - the HTML min="" attribute is not a control, anyone
    # can POST straight to Django. MIN_PRICE is also enforced by a DB constraint.
    original_price = models.DecimalField(
        max_digits=8, decimal_places=2, blank=True, null=True,
        validators=[MinValueValidator(Decimal('0'))],
    )
    price = models.DecimalField(
        max_digits=8, decimal_places=2,
        validators=[MinValueValidator(MIN_PRICE, message=_('The price must be at least \u20b91.'))],
    )
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    description = models.TextField(blank=True)

    is_rentable = models.BooleanField(
        default=False,
        help_text='Physical books only. Lets buyers rent this copy (7/15/30 days) instead of buying it, '
                   'priced as a % of the buy price above.',
    )

    pickup_address = models.CharField(max_length=255, blank=True)
    pickup_city = models.CharField(max_length=100, blank=True)
    pickup_pincode = models.CharField(max_length=10, blank=True)
    pickup_latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    pickup_longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    is_featured = models.BooleanField(default=False, help_text='Permanent feature flag, set manually in Django admin.')
    featured_until = models.DateTimeField(
        null=True, blank=True,
        help_text='Time-boxed "bump" a seller paid for via books:bump_listing - see is_currently_featured().',
    )
    views = models.PositiveIntegerField(default=0)

    # Short reservation while a buyer is on the online-payment page, so two buyers can't both pay
    # for the one physical copy (orders.views._reserve_for_payment). Expires by itself:
    # a hold only counts while payment_reserved_until is in the future.
    payment_reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', editable=False,
    )
    payment_reserved_until = models.DateTimeField(null=True, blank=True, editable=False)

    # --- AI Fraud Detection --------------------------------------------
    # Set by books.fraud_detection.run_fraud_scan() at publish/edit time -
    # see books.views.add_book/edit_book. fraud_flags is a JSON-encoded
    # list of {'check', 'severity', 'message'} dicts (kept as a plain
    # TextField rather than a related model, same "one row, no extra
    # table" approach as the rest of this app's lightweight AI features).
    fraud_score = models.PositiveSmallIntegerField(default=0)
    fraud_flags = models.TextField(blank=True)
    fraud_checked_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=1), name='book_price_gte_one'),
            models.CheckConstraint(
                condition=models.Q(original_price__isnull=True) | models.Q(original_price__gte=0),
                name='book_original_price_gte_zero',
            ),
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name='book_quantity_gte_one'),
        ]

    def payment_hold_active(self, now=None):
        return bool(self.payment_reserved_until and self.payment_reserved_until > (now or timezone.now()))

    def is_payment_held_for_other(self, user):
        """True while ANOTHER buyer is in the middle of paying for this listing."""
        return self.payment_hold_active() and self.payment_reserved_by_id != getattr(user, 'pk', None)

    def __str__(self):
        return f"{self.title} ({self.get_condition_display()}) - ₹{self.price}"

    def get_absolute_url(self):
        return reverse('books:detail', args=[self.pk])

    @property
    def is_digital(self):
        return self.listing_type == 'digital'

    @property
    def is_under_review(self):
        return self.status == 'under_review'

    def get_fraud_flags(self):
        """Decoded fraud_flags for templates/admin - see fraud_detection.run_fraud_scan()."""
        if not self.fraud_flags:
            return []
        try:
            return json.loads(self.fraud_flags)
        except ValueError:
            return []

    def set_fraud_result(self, result):
        """Stores a fraud_detection.run_fraud_scan() result on this instance
        (caller is responsible for saving the relevant fields)."""
        from django.utils import timezone
        self.fraud_score = result['score']
        self.fraud_flags = json.dumps(result['flags'])
        self.fraud_checked_at = timezone.now()

    @property
    def discount_percent(self):
        if self.original_price and self.original_price > 0:
            return round((1 - float(self.price) / float(self.original_price)) * 100)
        return None

    def is_currently_featured(self):
        """True for admin's permanent is_featured flag OR an active paid
        bump (see books.views.bump_listing / FEATURED_BUMP_OPTIONS)."""
        from django.utils import timezone
        if self.is_featured:
            return True
        return bool(self.featured_until and self.featured_until >= timezone.now())

    @property
    def suggested_price(self):
        """Simple 'AI-style' price suggestion: average price of similar listings."""
        qs = Book.objects.filter(title__iexact=self.title, status='available').exclude(pk=self.pk)
        agg = qs.aggregate(avg=Avg('price'))
        return round(agg['avg'], 2) if agg['avg'] else None

    def main_image(self):
        img = self.images.first()
        return img.image.url if img else None

    @property
    def can_be_rented(self):
        return self.is_rentable and not self.is_digital

    def rent_price(self, days):
        """Rent price for a given duration, or None if not a valid/rentable option."""
        if not self.can_be_rented:
            return None
        percent = RENTAL_DURATION_PERCENTAGES.get(days)
        if percent is None:
            return None
        return (self.price * percent / Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

    @property
    def rental_options(self):
        """[{'days': 7, 'price': Decimal}, ...] for the book_detail rent picker, cheapest first."""
        if not self.can_be_rented:
            return []
        return [
            {'days': days, 'price': self.rent_price(days)}
            for days in sorted(RENTAL_DURATION_PERCENTAGES)
        ]


class BookImage(models.Model):
    ANGLE_CHOICES = (
        ('front', _('Front Cover')),
        ('back', _('Back Cover')),
        ('side', _('Side / Spine')),
        ('pages', _('Sample Pages')),
    )
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='books/')
    angle = models.CharField(max_length=10, choices=ANGLE_CHOICES, default='front')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # 64-bit average-hash fingerprint, computed once here at upload time so
    # the reused-photo check in books/fraud_detection.py can compare integers
    # instead of re-decoding a few hundred JPEGs with Pillow inside every
    # publish/edit request. Null means "not hashed yet" (rows that predate
    # this column, or an image Pillow couldn't read).
    image_hash = models.BigIntegerField(blank=True, null=True, db_index=True)

    def __str__(self):
        return f"{self.book.title} - {self.angle}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.image_hash is None and self.image:
            from .fraud_detection import compute_image_hash
            image_hash = compute_image_hash(self.image)
            if image_hash is not None:
                # Signed 64-bit column, unsigned 64-bit hash - shift it into
                # range so Postgres accepts it. _hamming_distance works on
                # the XOR either way.
                BookImage.objects.filter(pk=self.pk).update(image_hash=image_hash - 2 ** 63)
                self.image_hash = image_hash - 2 ** 63


class Wishlist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wishlist')
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='wishlisted_by')
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'book')


class RecentlyViewed(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recently_viewed')
    book = models.ForeignKey(Book, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'book')
        ordering = ['-viewed_at']


class BookRequest(models.Model):
    STATUS_CHOICES = (
        ('open', _('Open')),
        ('fulfilled', _('Fulfilled')),
        ('closed', _('Closed')),
    )
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='book_requests')
    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255, blank=True)
    isbn = models.CharField(max_length=20, blank=True)
    edition = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    max_price = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    location = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Request: {self.title} by {self.buyer.username}"


class SavedSearch(models.Model):
    """
    A buyer's saved search (keyword + filters, mirroring the book_list query
    params). When a new book is listed that matches, the buyer gets a
    notification instead of having to keep re-running the search - see
    books.matching.find_matching_saved_searches(), called from
    books.views.add_book at the point a new listing is published.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_searches')
    query = models.CharField(max_length=255, blank=True, help_text='Free-text keywords, same as the search box.')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    board = models.CharField(max_length=100, blank=True)
    university = models.CharField(max_length=150, blank=True)
    semester = models.CharField(max_length=20, blank=True)
    condition = models.CharField(max_length=20, choices=Book.CONDITION_CHOICES, blank=True)
    city = models.CharField(max_length=100, blank=True)
    min_price = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    max_price = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    listing_type = models.CharField(max_length=10, choices=Book.LISTING_TYPE_CHOICES, blank=True)
    digital_type = models.CharField(max_length=20, choices=Book.DIGITAL_TYPE_CHOICES, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username}: {self.label}"

    @property
    def label(self):
        """Short human-readable summary for emails and the manage-searches page."""
        bits = [self.query] if self.query else []
        bits += [b for b in (self.board, self.university, self.semester, self.city) if b]
        return ' · '.join(bits) if bits else 'all new listings'

    def matches(self, book):
        """Mirrors books.views.book_list's filtering logic for a single book."""
        if self.query:
            q = self.query.lower()
            haystack = ' '.join(filter(None, [
                book.title, book.author, book.subject, book.isbn,
                book.university, book.board, book.branch, book.semester,
            ])).lower()
            if q not in haystack:
                return False
        if self.category_id and book.category_id != self.category_id:
            return False
        if self.board and self.board.lower() not in (book.board or '').lower():
            return False
        if self.university and self.university.lower() not in (book.university or '').lower():
            return False
        if self.semester and self.semester.lower() not in (book.semester or '').lower():
            return False
        if self.condition and self.condition != book.condition:
            return False
        if self.city and self.city.lower() not in (book.pickup_city or '').lower():
            return False
        if self.min_price is not None and book.price < self.min_price:
            return False
        if self.max_price is not None and book.price > self.max_price:
            return False
        if self.listing_type and self.listing_type != book.listing_type:
            return False
        if self.digital_type and self.digital_type != book.digital_type:
            return False
        return True


class ListingReport(models.Model):
    """
    A buyer-initiated flag on a listing they think is fake, spam, or
    otherwise wrong - the manual counterpart to the automated scoring in
    books/fraud_detection.py. Purely informational: filing a report never
    changes the book's status itself (that would let anyone take down a
    competitor's listing by spamming reports) - it just surfaces the
    listing on the Staff Dashboard (adminpanel) for a human to look at.
    See adminpanel/views.py:dismiss_report and the existing
    approve_book/remove_book actions for how staff act on these.
    """
    REASON_CHOICES = (
        ('fake', _('Fake / Counterfeit')),
        ('scam', _('Looks like a scam')),
        ('wrong_category', _('Wrong Category / Mislabeled')),
        ('offensive', _('Offensive Content')),
        ('other', _('Other')),
    )
    STATUS_CHOICES = (
        ('pending', _('Pending Review')),
        ('dismissed', _('Dismissed')),
        ('actioned', _('Listing Removed')),
    )

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='listing_reports',
        help_text='Kept even if the reporting account is later deleted, so the report itself survives.',
    )
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    details = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Report on "{self.book.title}" ({self.get_reason_display()}) - {self.status}'
