from django.contrib import admin

from .models import Donation, DonationCertificate, DonationItem, DonationPartner


@admin.register(DonationPartner)
class DonationPartnerAdmin(admin.ModelAdmin):
    list_display = ('name', 'org_type', 'city', 'accepts_pickup', 'is_active')
    list_filter = ('org_type', 'is_active', 'accepts_pickup', 'city')
    search_fields = ('name', 'city', 'registration_number')


class DonationItemInline(admin.TabularInline):
    model = DonationItem
    extra = 0


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = ('id', 'donor', 'partner', 'status', 'received_count', 'created_at')
    list_filter = ('status', 'partner')
    search_fields = ('donor__username', 'certificate_name', 'partner__name')
    inlines = [DonationItemInline]
    # Status changes (and certificate issuing) go through the staff pages / services so
    # that notifications, audit log and the state rules always apply.
    readonly_fields = ('status', 'received_count', 'reviewed_by', 'reviewed_at', 'created_at', 'updated_at')


@admin.register(DonationCertificate)
class DonationCertificateAdmin(admin.ModelAdmin):
    list_display = ('code', 'recipient_name', 'partner_name', 'books_count', 'issued_at', 'revoked')
    list_filter = ('revoked',)
    search_fields = ('code', 'recipient_name', 'partner_name')
    readonly_fields = ('code', 'donation', 'recipient_name', 'partner_name', 'partner_city', 'books_count',
                       'issued_at', 'revoked', 'revoked_reason', 'revoked_at')

    def has_add_permission(self, request):
        return False
