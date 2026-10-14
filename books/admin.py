from django.contrib import admin
from .models import Category, Book, BookImage, Wishlist, RecentlyViewed, BookRequest, SavedSearch, ListingReport


class BookImageInline(admin.TabularInline):
    model = BookImage
    extra = 1


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'group')
    list_filter = ('group',)
    search_fields = ('name',)


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'seller', 'category', 'condition', 'price', 'is_rentable', 'status',
        'fraud_score', 'is_featured', 'views', 'created_at',
    )
    list_filter = ('status', 'condition', 'category', 'board', 'is_featured', 'is_rentable')
    search_fields = ('title', 'author', 'isbn', 'subject', 'university')
    inlines = [BookImageInline]
    list_editable = ('is_featured',)
    # digital_file is a private file (no public URL) - the default file widget would crash on it.
    exclude = ('digital_file',)
    readonly_fields = ('fraud_score', 'fraud_flags', 'fraud_flags_display', 'fraud_checked_at', 'has_digital_file')

    @admin.display(boolean=True, description='Digital file attached')
    def has_digital_file(self, obj):
        return bool(obj and obj.digital_file)
    actions = ['mark_removed', 'mark_featured', 'approve_from_review']

    def fraud_flags_display(self, obj):
        """Fraud Detection's flags, one per line, for the admin detail view."""
        flags = obj.get_fraud_flags()
        if not flags:
            return 'No flags.'
        return '\n'.join(f"[{f['severity']}] {f['check']}: {f['message']}" for f in flags)
    fraud_flags_display.short_description = 'Fraud Detection flags'

    def mark_removed(self, request, queryset):
        queryset.update(status='removed')
    mark_removed.allowed_permissions = ('change',)
    mark_removed.short_description = 'Mark selected books as removed (fraud/spam)'

    def mark_featured(self, request, queryset):
        queryset.update(is_featured=True)
    mark_featured.allowed_permissions = ('change',)
    mark_featured.short_description = 'Feature selected books'

    def approve_from_review(self, request, queryset):
        queryset.filter(status='under_review').update(status='available')
    approve_from_review.allowed_permissions = ('change',)
    approve_from_review.short_description = 'Approve selected listings held under review'


@admin.register(BookRequest)
class BookRequestAdmin(admin.ModelAdmin):
    list_display = ('title', 'buyer', 'max_price', 'location', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('title', 'author', 'isbn')


@admin.register(SavedSearch)
class SavedSearchAdmin(admin.ModelAdmin):
    list_display = ('user', 'label', 'created_at', 'last_notified_at')
    search_fields = ('user__username', 'query')


@admin.register(ListingReport)
class ListingReportAdmin(admin.ModelAdmin):
    list_display = ('book', 'reporter', 'reason', 'status', 'created_at')
    list_filter = ('status', 'reason')
    search_fields = ('book__title', 'reporter__username', 'details')


admin.site.register(Wishlist)
admin.site.register(RecentlyViewed)
