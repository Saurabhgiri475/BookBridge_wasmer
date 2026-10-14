from django.urls import path
from . import views

app_name = 'adminpanel'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('revenue/', views.revenue_dashboard, name='revenue_dashboard'),
    path('books/<int:pk>/approve/', views.approve_book, name='approve_book'),
    path('books/<int:pk>/remove/', views.remove_book, name='remove_book'),
    path('books/<int:pk>/clear-fraud-flag/', views.clear_fraud_flag, name='clear_fraud_flag'),
    path('users/<int:pk>/clear-duplicate-flag/', views.clear_duplicate_flag, name='clear_duplicate_flag'),
    path('reports/<int:pk>/dismiss/', views.dismiss_report, name='dismiss_report'),
    path('partners/<int:pk>/approve/', views.approve_partner, name='approve_partner'),
    path('partners/<int:pk>/reject/', views.reject_partner, name='reject_partner'),
]
