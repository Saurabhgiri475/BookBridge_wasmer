from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import BuybackRequest, BuybackRequestImage, PartnerCommission, PartnerReview, ShopkeeperPartner


@admin.register(ShopkeeperPartner)
class ShopkeeperPartnerAdmin(admin.ModelAdmin):
    list_display = ('shop_name', 'user', 'shop_city', 'status', 'commission_percent', 'is_accepting_requests', 'applied_at')
    list_filter = ('status', 'is_accepting_requests', 'shop_city')
    search_fields = ('shop_name', 'user__username', 'shop_city', 'gstin')
    # The ID/shop proofs are private files: no public URL exists, so the default
    # file widget (which links to .url) can't render them. Staff open them through
    # the audited view instead.
    exclude = ('owner_id_proof', 'shop_proof')
    readonly_fields = ('owner_aadhaar_hash', 'owner_aadhaar_last4', 'applied_at', 'id_proof_link', 'shop_proof_link')

    @admin.display(description='ID proof')
    def id_proof_link(self, obj):
        if not (obj and obj.pk and obj.owner_id_proof):
            return '-'
        return format_html('<a href="{}" target="_blank" rel="noopener">Open (access is logged)</a>',
                           reverse('partners:proof_file', args=[obj.pk, 'id']))

    @admin.display(description='Shop proof')
    def shop_proof_link(self, obj):
        if not (obj and obj.pk and obj.shop_proof):
            return '-'
        return format_html('<a href="{}" target="_blank" rel="noopener">Open (access is logged)</a>',
                           reverse('partners:proof_file', args=[obj.pk, 'shop']))


class BuybackRequestImageInline(admin.TabularInline):
    model = BuybackRequestImage
    extra = 0
    # Private files have no public URL, so the default file widget cannot render them.
    exclude = ('image',)
    readonly_fields = ('photo_link',)
    can_delete = True

    @admin.display(description='Photo')
    def photo_link(self, obj):
        if not (obj and obj.pk and obj.image):
            return '-'
        return format_html('<a href="{}" target="_blank" rel="noopener">Open</a>',
                           reverse('partners:buyback_image', args=[obj.pk]))

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BuybackRequest)
class BuybackRequestAdmin(admin.ModelAdmin):
    list_display = ('title', 'seller', 'partner', 'status', 'offered_price', 'final_price', 'pickup_city', 'created_at')
    list_filter = ('status', 'pickup_city')
    search_fields = ('title', 'seller__username', 'partner__shop_name')
    inlines = [BuybackRequestImageInline]
    readonly_fields = ('assigned_partner', 'assignment_expires_at', 'assignment_round', 'offered_at')


@admin.register(PartnerCommission)
class PartnerCommissionAdmin(admin.ModelAdmin):
    list_display = ('partner', 'buyback_request', 'amount', 'is_settled', 'settled_at', 'created_at')
    list_filter = ('is_settled',)
    search_fields = ('partner__shop_name',)


@admin.register(PartnerReview)
class PartnerReviewAdmin(admin.ModelAdmin):
    list_display = ('partner', 'rating', 'reviewer', 'is_hidden', 'created_at')
    list_filter = ('is_hidden', 'rating')
    search_fields = ('partner__shop_name', 'reviewer__username', 'comment')
    readonly_fields = ('partner', 'buyback_request', 'reviewer', 'rating', 'comment', 'created_at')
    actions = ['hide_reviews', 'unhide_reviews']

    @admin.action(description='Hide selected reviews (exclude from ratings and badges)', permissions=['change'])
    def hide_reviews(self, request, queryset):
        queryset.update(is_hidden=True)

    @admin.action(description='Unhide selected reviews', permissions=['change'])
    def unhide_reviews(self, request, queryset):
        queryset.update(is_hidden=False)
