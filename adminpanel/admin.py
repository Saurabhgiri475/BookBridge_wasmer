from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action', 'target_repr', 'ip_address')
    list_filter = ('action',)
    search_fields = ('target_repr', 'actor__username', 'note', 'reason', 'ip_address')
    readonly_fields = ('actor', 'action', 'target_repr', 'target_id', 'note', 'reason', 'old_value', 'new_value',
                       'ip_address', 'user_agent', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # append-only: also blocked in the model and queryset
