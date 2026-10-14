from django.contrib import admin
from .models import Wallet, WalletTransaction


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'updated_at')
    search_fields = ('user__username', 'user__email')


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('wallet', 'txn_type', 'source', 'amount', 'balance_after', 'order', 'created_at')
    list_filter = ('txn_type', 'source')
    search_fields = ('wallet__user__username', 'description')
    readonly_fields = ('wallet', 'txn_type', 'source', 'amount', 'balance_after', 'order', 'created_at')
