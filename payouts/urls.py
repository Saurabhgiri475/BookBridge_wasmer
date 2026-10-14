from django.urls import path
from . import views

app_name = 'payouts'

urlpatterns = [
    # Seller
    path('', views.my_payouts, name='mine'),
    path('upi/', views.update_upi, name='update_upi'),
    path('statement/pdf/', views.seller_statement_pdf, name='statement_pdf'),
    path('statement/csv/', views.seller_statement_csv, name='statement_csv'),
    # Shopkeeper partner (commission owed to BookBridge)
    path('partner-statement/pdf/', views.partner_statement_pdf, name='partner_statement_pdf'),
    path('partner-statement/csv/', views.partner_statement_csv, name='partner_statement_csv'),
    # Staff
    path('staff/', views.staff_queue, name='staff_queue'),
    path('staff/export/', views.staff_export_csv, name='staff_export'),
    path('staff/mark-paid/', views.staff_mark_paid, name='staff_mark_paid'),
    path('staff/<int:pk>/', views.staff_payout_detail, name='staff_detail'),
    path('staff/<int:pk>/action/', views.staff_payout_action, name='staff_action'),
    path('staff/<int:pk>/upi-name/', views.staff_check_upi_name, name='staff_check_upi_name'),
    path('staff/<int:pk>/upi-hold-override/', views.staff_override_upi_hold, name='staff_override_upi_hold'),
]
