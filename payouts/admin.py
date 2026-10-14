from django.contrib import admin

from .models import Payout, PayoutProfile


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    """Read-mostly: statuses must change through the Staff Payouts page so each
    change is validated and audit-logged, not edited free-hand here."""
    list_display = ('order_ref', 'seller', 'net_amount', 'status', 'utr', 'released_at', 'paid_at')
    list_filter = ('status', 'refunded_after_payout')
    search_fields = ('order_ref', 'seller__username', 'utr', 'book_title')
    readonly_fields = [f.name for f in Payout._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PayoutProfile)
class PayoutProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'upi_id', 'updated_at')
    search_fields = ('user__username', 'upi_id')
