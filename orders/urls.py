from django.urls import path
from . import views

app_name = 'orders'

urlpatterns = [
    path('offer/<int:book_pk>/make/', views.make_offer, name='make_offer'),
    path('offer/<int:offer_pk>/respond/', views.respond_offer, name='respond_offer'),
    path('checkout/<int:book_pk>/', views.checkout, name='checkout'),
    path('checkout/<int:book_pk>/apply-coupon/', views.apply_coupon, name='apply_coupon'),

    path('order/<uuid:order_id>/pay/', views.pay, name='pay'),
    path('order/<uuid:order_id>/pay/callback/', views.payment_callback, name='payment_callback'),
    path('webhook/razorpay/', views.razorpay_webhook, name='razorpay_webhook'),
    path('webhook/shiprocket/', views.shiprocket_webhook, name='shiprocket_webhook'),

    path('order/<uuid:order_id>/', views.order_detail, name='detail'),
    path('order/<uuid:order_id>/invoice/', views.download_invoice, name='download_invoice'),
    path('order/<uuid:order_id>/download/', views.download_digital_file, name='download_digital'),
    path('order/<uuid:order_id>/status/', views.update_order_status, name='update_status'),
    path('order/<uuid:order_id>/track/refresh/', views.refresh_tracking, name='refresh_tracking'),
    path('order/<uuid:order_id>/confirm/', views.confirm_delivery, name='confirm_delivery'),
    path('order/<uuid:order_id>/mark-returned/', views.mark_book_returned, name='mark_returned'),
    path('order/<uuid:order_id>/review/', views.leave_review, name='leave_review'),
    path('order/<uuid:order_id>/return/', views.request_return, name='request_return'),
    path('my-orders/', views.my_orders, name='my_orders'),
]
