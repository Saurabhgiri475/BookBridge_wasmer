from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'role', 'city', 'phone', 'is_premium_seller', 'email_verified', 'is_duplicate_flagged', 'is_active', 'date_joined')
    list_filter = ('role', 'is_premium_seller', 'email_verified', 'is_duplicate_flagged', 'is_active', 'signup_pending', 'city')
    search_fields = ('username', 'email', 'phone', 'referral_code')
    fieldsets = UserAdmin.fieldsets + (
        ('Marketplace Info', {'fields': ('role', 'phone', 'phone_verified', 'address', 'city', 'pincode', 'avatar', 'is_premium_seller', 'premium_until')}),
        ('Seller Verification', {'fields': ('email_verified',)}),
        ('Fraud Detection', {'fields': ('is_duplicate_flagged', 'duplicate_flag_reason')}),
        ('Referrals', {'fields': ('referral_code', 'referred_by', 'referral_bonus_paid')}),
    )
