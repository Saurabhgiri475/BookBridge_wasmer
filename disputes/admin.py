from django.contrib import admin

from .models import Dispute, DisputeEvidence, DisputeMessage


class MessageInline(admin.TabularInline):
    model = DisputeMessage
    extra = 0
    can_delete = False
    readonly_fields = ('author', 'role', 'body', 'internal', 'created_at')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    """Read-only: a dispute only changes through the Staff Disputes pages, so every
    decision is validated, moves money in one place and is audit-logged."""
    list_display = ('reference', 'book_title', 'buyer', 'seller', 'category', 'status', 'outcome', 'refund_amount', 'created_at')
    list_filter = ('status', 'category', 'outcome')
    search_fields = ('order_ref', 'book_title', 'buyer__username', 'seller__username')
    readonly_fields = [f.name for f in Dispute._meta.fields]
    inlines = [MessageInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(DisputeEvidence)
