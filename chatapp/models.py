from django.conf import settings
from django.db import models
from bookmarket.storage import get_private_storage


class Conversation(models.Model):
    """A chat thread between two users about EITHER a marketplace book
    (book set) OR a buyback lead (buyback set) - never both.

    For a buyback chat the roles map as: `seller` = the person selling the
    book to BookBridge, `buyer` = the Shopkeeper Partner's user account (the
    side that is buying it). Keeping the same two columns means the inbox,
    access check and notification code work unchanged for both kinds."""
    book = models.ForeignKey(
        'books.Book', on_delete=models.CASCADE, related_name='conversations', null=True, blank=True,
    )
    buyback = models.ForeignKey(
        'partners.BuybackRequest', on_delete=models.CASCADE, related_name='conversations',
        null=True, blank=True,
    )
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversations_as_buyer')
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversations_as_seller')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('book', 'buyer', 'seller')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['buyback', 'buyer', 'seller'], condition=models.Q(buyback__isnull=False),
                name='unique_buyback_conversation',
            ),
            models.CheckConstraint(
                condition=(models.Q(book__isnull=False, buyback__isnull=True)
                           | models.Q(book__isnull=True, buyback__isnull=False)),
                name='conversation_book_xor_buyback',
            ),
        ]

    @property
    def is_buyback(self):
        return self.buyback_id is not None

    @property
    def subject_title(self):
        return self.buyback.title if self.buyback_id else self.book.title

    def other_party(self, user):
        return self.seller if user == self.buyer else self.buyer

    def __str__(self):
        kind = 'buyback ' if self.buyback_id else ''
        return f"{self.buyer} <-> {self.seller} about {kind}{self.subject_title}"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.TextField(blank=True)
    image = models.ImageField(upload_to='chat_images/', storage=get_private_storage, blank=True, null=True)
    shared_location = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.sender}: {self.text[:30]}"
