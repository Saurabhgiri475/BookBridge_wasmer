from django.urls import path
from . import views

app_name = 'books'

urlpatterns = [
    path('', views.home, name='home'),
    path('about/', views.about, name='about'),
    path('contact/', views.contact, name='contact'),
    path('impact/', views.sustainability_dashboard, name='sustainability'),
    path('browse/', views.book_list, name='list'),
    path('book/<int:pk>/', views.book_detail, name='detail'),
    path('sell/', views.add_book, name='add'),
    path('sell/bulk/', views.bulk_upload_books, name='bulk_upload'),
    path('sell/bulk/template/', views.bulk_upload_template, name='bulk_upload_template'),
    path('sell/scan-cover/', views.scan_cover, name='scan_cover'),
    path('sell/scan-isbn/', views.scan_isbn, name='scan_isbn'),
    path('sell/price-recommendation/', views.price_recommendation, name='price_recommendation'),
    path('sell/check-duplicate/', views.check_duplicate, name='check_duplicate'),
    path('book/<int:pk>/edit/', views.edit_book, name='edit'),
    path('book/<int:pk>/delete/', views.delete_book, name='delete'),
    path('book/<int:pk>/report/', views.report_book, name='report'),
    path('book/<int:pk>/bump/', views.bump_listing, name='bump'),
    path('book/<int:pk>/wishlist/', views.toggle_wishlist, name='toggle_wishlist'),
    path('wishlist/', views.wishlist_view, name='wishlist'),
    path('saved-searches/', views.saved_search_list, name='saved_search_list'),
    path('saved-searches/save/', views.save_search, name='save_search'),
    path('saved-searches/<int:pk>/delete/', views.delete_saved_search, name='delete_saved_search'),
    path('requests/', views.request_list, name='request_list'),
    path('requests/new/', views.add_request, name='add_request'),
    path('requests/matching/', views.matching_requests_for_seller, name='matching_requests'),
]
