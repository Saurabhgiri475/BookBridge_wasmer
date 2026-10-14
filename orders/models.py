import uuid
from django.conf import settings
from decimal import Decimal
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

MAX_DELIVERY_OTP_ATTEMPTS = 5  # wrong guesses allowed before a shipment's delivery OTP is locked out


class Offer(models.Model):
    STATUS_CHOICES = (
        ('pending', _('Pending')),
        ('countered', _('Countered')),
        ('accepted', _('Accepted')),
        ('rejected', _('Rejected')),
    )
    book = models.ForeignKey('books.Book', on_delete=models.CASCADE, related_name='offers')
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='offers_made')
    amount = models.DecimalField(
        max_digits=8, decimal_places=2,
        validators=[MinValueValidator(Decimal('1.00'), message=_('An offer must be at least \u20b91.'))],
    )
    made_by = models.CharField(max_length=10, choices=(('buyer', _('Buyer')), ('seller', _('Seller'))), default='buyer')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=1), name='offer_amount_gte_one'),
        ]

    def __str__(self):
        return f"Offer ₹{self.amount} on {self.book.title} ({self.status})"


class Order(models.Model):
    STATUS_CHOICES = (
        ('placed', _('Placed')),
        ('accepted', _('Accepted by Seller')),
        ('shipped', _('Shipped')),
        ('out_for_delivery', _('Out for Delivery')),
        ('delivered', _('Delivered')),
        ('cancelled', _('Cancelled')),
        ('returned', _('Returned')),
    )
    ORDER_TYPE_CHOICES = (
        ('purchase', _('Purchase')),
        ('rental', _('Rental')),
    )

    order_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='orders_bought')
    book = models.ForeignKey('books.Book', on_delete=models.CASCADE, related_name='orders')
    final_price = models.DecimalField(max_digits=8, decimal_places=2)
    delivery_charge = models.DecimalField(max_digits=8, decimal_places=2, default=40)
    platform_commission_percent = models.DecimalField(max_digits=4, decimal_places=1, default=10.0)

    # --- Rental --------------------------------------------------------
    # order_type is set once at checkout (see orders/views.checkout()) from
    # the rent_days query param / hidden field - final_price above already
    # holds the rent price (book.rent_price(rental_days)), not the buy
    # price, once order_type == 'rental'. rental_due_date is filled in when
    # the order reaches 'delivered' (confirm_delivery()), not at checkout,
    # since the rental clock should start once the buyer actually has the
    # book in hand.
    order_type = models.CharField(max_length=10, choices=ORDER_TYPE_CHOICES, default='purchase')
    rental_days = models.PositiveSmallIntegerField(blank=True, null=True)
    rental_due_date = models.DateTimeField(blank=True, null=True)

    # --- Coupon ------------------------------------------------------------
    # Set together, once, at checkout (see orders/views.checkout ->
    # coupons.services.validate_coupon/redeem_coupon). coupon_discount comes
    # off what the buyer pays; it never touches final_price or
    # seller_payout below, so the platform (not the seller) funds the
    # discount - the coupon itself is kept on SET_NULL so a later-deleted
    # Coupon doesn't take historical orders down with it.
    coupon = models.ForeignKey('coupons.Coupon', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    coupon_discount = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Blank for digital orders (no shipping) - see orders/views.checkout(),
    # which skips shipment creation and sets delivery_charge to 0 for those.
    delivery_address = models.CharField(max_length=255, blank=True)
    delivery_city = models.CharField(max_length=100, blank=True)
    delivery_pincode = models.CharField(max_length=10, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='placed')
    # Set once, when the order is confirmed delivered (see orders.views). The
    # dispute window (disputes.services) counts from here - updated_at moves on every save.
    delivered_at = models.DateTimeField(blank=True, null=True)
    payment_method = models.CharField(
        max_length=20,
        choices=(
            ('cod', _('Cash on Delivery')), ('upi', _('UPI')), ('card', _('Card')), ('wallet', _('Wallet')),
            ('bb_wallet', _('BookBridge Wallet')),
        ),
        default='cod',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Order #{str(self.order_id)[:8]} - {self.book.title}"

    @property
    def is_digital(self):
        return self.book.is_digital

    @property
    def is_rental(self):
        return self.order_type == 'rental'

    @property
    def is_overdue(self):
        return bool(
            self.is_rental and self.rental_due_date and self.status == 'delivered'
            and timezone.now() > self.rental_due_date
        )

    @property
    def total_amount(self):
        return self.final_price + self.delivery_charge - self.coupon_discount

    @property
    def commission_amount(self):
        return round(self.final_price * self.platform_commission_percent / 100, 2)

    @property
    def seller_payout(self):
        return self.final_price - self.commission_amount


class Payment(models.Model):
    STATUS_CHOICES = (
        ('created', _('Razorpay Order Created')),
        ('pending', _('Pending')),
        ('escrow_held', _('Held in Escrow')),
        ('released', _('Released to Seller')),
        ('refunded', _('Refunded')),
        ('failed', _('Failed')),
    )
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='payment')
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    method = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    transaction_ref = models.CharField(max_length=100, blank=True)

    # Razorpay linkage. For COD orders these stay blank - no gateway payment
    # ever happens, so `status` is managed locally instead.
    razorpay_order_id = models.CharField(max_length=100, blank=True, db_index=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, db_index=True)
    razorpay_signature = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Payment for {self.order} - {self.status}"


class Shipment(models.Model):
    STATUS_CHOICES = (
        ('pickup_pending', _('Pickup Pending')),
        ('picked_up', _('Picked Up')),
        ('in_transit', _('In Transit')),
        ('out_for_delivery', _('Out for Delivery')),
        ('delivered', _('Delivered')),
        ('failed', _('Delivery Failed')),
        ('cancelled', _('Cancelled')),
    )
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='shipment')

    # Shiprocket linkage. courier_name/tracking_id are whatever Shiprocket's
    # courier-assignment call actually returns (real AWB number), not a
    # locally-generated fake ID.
    shiprocket_order_id = models.CharField(max_length=50, blank=True)
    shiprocket_shipment_id = models.CharField(max_length=50, blank=True)
    courier_name = models.CharField(max_length=100, blank=True)
    tracking_id = models.CharField(max_length=40, blank=True, null=True, unique=True,
                                    help_text='AWB code assigned by the courier via Shiprocket')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pickup_pending')
    delivery_otp = models.CharField(max_length=6, blank=True)
    # Wrong-guess counter for delivery_otp - this OTP gates escrow payment
    # release, so it's locked out after MAX_DELIVERY_OTP_ATTEMPTS wrong
    # tries rather than allowing unlimited guesses at a 6-digit code. See
    # check_delivery_otp() below and orders.views.confirm_delivery.
    delivery_otp_attempts = models.PositiveSmallIntegerField(default=0)

    # Raw tracking activity as returned by Shiprocket's track API, refreshed
    # on demand (or by the Shiprocket webhook) - JSON-encoded list of
    # {date, status, activity, location} entries, newest first.
    tracking_history = models.TextField(blank=True)
    tracking_synced_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tracking_id or 'unassigned'} ({self.get_status_display()})"

    def check_delivery_otp(self, submitted_otp):
        """Locks out further guesses once MAX_DELIVERY_OTP_ATTEMPTS wrong
        codes have been submitted for this shipment - the buyer/seller can
        still resolve it through support, but the endpoint stops being a
        free brute-force oracle against a 6-digit code guarding escrow
        release."""
        if not self.delivery_otp:
            return False
        if self.delivery_otp_attempts >= MAX_DELIVERY_OTP_ATTEMPTS:
            return False

        if self.delivery_otp == submitted_otp:
            return True

        self.delivery_otp_attempts += 1
        self.save(update_fields=['delivery_otp_attempts'])
        return False

    def tracking_events(self):
        import json
        if not self.tracking_history:
            return []
        try:
            return json.loads(self.tracking_history)
        except (ValueError, TypeError):
            return []


class Review(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='review')
    seller_rating = models.PositiveSmallIntegerField(default=5)
    book_quality_rating = models.PositiveSmallIntegerField(default=5)
    delivery_rating = models.PositiveSmallIntegerField(default=5)
    buyer_rating = models.PositiveSmallIntegerField(default=5, help_text='Given by seller about buyer')
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Review for {self.order}"


class ReturnRequest(models.Model):
    REASON_CHOICES = (
        ('wrong_book', _('Wrong Book')),
        ('damaged', _('Damaged')),
        ('missing_pages', _('Missing Pages')),
        ('other', _('Other')),
    )
    STATUS_CHOICES = (
        ('requested', _('Requested')),
        ('approved', _('Approved')),
        ('rejected', _('Rejected')),
        ('refunded', _('Refunded')),
    )
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='return_request')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    details = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Return for {self.order} - {self.status}"


class RefundRecord(models.Model):
    """
    The refund ledger: at most ONE refund per order, enforced by the database.

    Every refund route (return-request refunds in the admin, dispute resolutions) first claims
    this row. Two staff members clicking at once, a retry after a timeout, or a return refund
    racing a dispute refund can't both get it, so the buyer can't be paid twice. See
    orders/refund_ledger.py.
    """
    DESTINATION_CHOICES = (('wallet', _('BookBridge wallet')), ('original', _('Original payment method')))
    SOURCE_CHOICES = (('return_request', _('Return request')), ('dispute', _('Dispute')))
    STATUS_CHOICES = (('pending', _('Pending - claimed, money movement not confirmed')), ('done', _('Done')))

    order = models.OneToOneField(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='refund_record')
    order_ref = models.CharField(max_length=36, help_text='The order UUID, kept even if the order row is deleted.')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    destination = models.CharField(max_length=10, choices=DESTINATION_CHOICES)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    reference = models.CharField(max_length=100, blank=True, help_text='Razorpay refund id, or the wallet transaction.')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Refund {self.order_ref[:8]} - Rs {self.amount} ({self.status})'
