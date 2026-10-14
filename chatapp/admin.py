from django.contrib import admin
from .models import Conversation, Message


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'book', 'buyback', 'created_at')
    raw_id_fields = ('book', 'buyback', 'buyer', 'seller')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('conversation', 'sender', 'created_at')
    raw_id_fields = ('conversation', 'sender')
    # Chat photos are private files with no public URL, so the default file widget
    # (which links to .url) cannot render them.
    exclude = ('image',)
    readonly_fields = ('image_note',)

    @admin.display(description='Photo')
    def image_note(self, obj):
        if not (obj and obj.pk and obj.image):
            return '-'
        return 'Attached (private - visible only to the two people in the chat).'
