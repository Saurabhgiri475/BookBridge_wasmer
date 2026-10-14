from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from bookmarket.storage import get_private_storage
from django.utils.translation import gettext_lazy as _

ACTIVE_STATUSES = ('open', 'seller_responded', 'escalated')


class Dispute(models.Model):
    """
    A buyer's complaint about one order, from "the seller gets a chance to
    sort it out" through to a BookBridge staff decision.

    Lifecycle:  open -> (seller_responded) -> escalated -> resolved
                          \\-> withdrawn (buyer drops it, any time before resolved)

    The existing ReturnRequest (orders app) stays as the quick "I want to send
    this back" path; a dispute is for everything a return can't express - item
    never arrived, partial refunds, back-and-forth with the seller, photo
    evidence. Money only moves in disputes.services.staff_resolve.
    """
    CATEGORY_CHOICES = (
        ('not_received', _('Item not received')),
        ('not_as_described', _('Not as described')),
        ('damaged', _('Arrived damaged')),
        ('wrong_item', _('Wrong book sent')),
        ('missing_pages', _('Missing pages')),
        ('digital_file', _('Digital file problem')),
        ('other', _('Something else')),
    )
    WANTED_CHOICES = (
        ('full_refund', _('Full refund')),
        ('partial_refund', _('Partial refund')),
        ('replacement', _('Replacement')),
        ('undecided', _('Not sure - let BookBridge suggest')),
    )
    STATUS_CHOICES = (
        ('open', _('Waiting for seller')),
        ('seller_responded', _('Seller replied')),
        ('escalated', _('With BookBridge team')),
        ('resolved', _('Resolved')),
        ('withdrawn', _('Withdrawn')),
    )
    OUTCOME_CHOICES = (
        ('refund', _('Refund issued')),
        ('replacement', _('Replacement arranged')),
        ('rejected', _('Dispute declined')),
    )
    REFUND_DEST_CHOICES = (
        ('wallet', _('BookBridge Wallet')),
        ('original', _('Original payment method')),
    )

    # SET_NULL + snapshots: the dispute record must survive an order being deleted.
    order = models.ForeignKey('orders.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='disputes')
    order_ref = models.CharField(max_length=8, help_text='First 8 characters of the order id, as shown to users.')
    book_title = models.CharField(max_length=255)
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='disputes_opened')
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='disputes_against')

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    description = models.TextField()
    desired_resolution = models.CharField(max_length=20, choices=WANTED_CHOICES, default='undecided')
    requested_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', db_index=True)
    seller_response_due = models.DateTimeField()
    seller_agreed = models.BooleanField(default=False, help_text='The seller said they agree the buyer should be refunded.')
    escalated_at = models.DateTimeField(null=True, blank=True)
    auto_escalated = models.BooleanField(default=False, help_text='Escalated because the seller did not reply in time.')

    # Whether *this dispute* put the seller's payout on hold (so it only ever
    # releases a hold it placed, never one staff set for another reason).
    payout_held = models.BooleanField(default=False)

    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, blank=True)
    resolution_note = models.TextField(blank=True, help_text='Shown to both the buyer and the seller.')
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    refund_destination = models.CharField(max_length=10, choices=REFUND_DEST_CHOICES, blank=True)
    refund_reference = models.CharField(max_length=100, blank=True, help_text='Razorpay refund id or wallet transaction id.')
    deducted_from_seller = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='disputes_resolved',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'seller_response_due'])]
        constraints = [
            models.UniqueConstraint(
                fields=['order'], condition=Q(status__in=ACTIVE_STATUSES), name='one_active_dispute_per_order',
            ),
        ]

    def __str__(self):
        return f'{self.reference} - {self.book_title} ({self.get_status_display()})'

    @property
    def reference(self):
        return f'DSP-{self.pk:06d}' if self.pk else 'DSP-new'

    @property
    def is_active(self):
        return self.status in ACTIVE_STATUSES

    @property
    def seller_overdue(self):
        return self.status == 'open' and timezone.now() >= self.seller_response_due

    @property
    def buyer_can_escalate(self):
        """The seller gets a fair window to answer; after they reply (or the
        window passes) the buyer can bring BookBridge in."""
        return self.status == 'seller_responded' or self.seller_overdue


class DisputeMessage(models.Model):
    ROLE_CHOICES = (
        ('buyer', _('Buyer')),
        ('seller', _('Seller')),
        ('staff', _('BookBridge')),
        ('system', _('System')),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='messages')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    body = models.TextField()
    internal = models.BooleanField(default=False, help_text='Staff-only note; never shown to the buyer or seller.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.dispute.reference} {self.role}: {self.body[:40]}'


class DisputeEvidence(models.Model):
    """A photo attached to a message. Stored privately (bookmarket/storage.py)
    and only readable through disputes.views.evidence_file."""
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='evidence')
    message = models.ForeignKey(DisputeMessage, on_delete=models.CASCADE, null=True, blank=True, related_name='evidence')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    file = models.FileField(upload_to='disputes/%Y/%m/', storage=get_private_storage)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'Evidence for {self.dispute.reference}'
