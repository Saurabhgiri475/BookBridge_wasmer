from django.contrib import admin
from .models import Coupon, CouponRedemption


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'discount_type', 'discount_value', 'max_discount_amount', 'min_order_amount',
        'usage_limit_per_user', 'usage_limit_total', 'redemption_count', 'active', 'valid_from', 'valid_until',
    )
    list_filter = ('active', 'discount_type', 'first_order_only')
    search_fields = ('code', 'description')
    readonly_fields = ('created_at',)

    def redemption_count(self, obj):
        return obj.total_redemptions()
    redemption_count.short_description = 'Times used'


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    list_display = ('coupon', 'user', 'order', 'amount_discounted', 'created_at')
    list_filter = ('coupon',)
    search_fields = ('coupon__code', 'user__username', 'user__email')
    readonly_fields = ('coupon', 'user', 'order', 'amount_discounted', 'created_at')
