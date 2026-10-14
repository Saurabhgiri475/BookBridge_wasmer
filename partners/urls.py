from django.urls import path

from . import views

app_name = 'partners'

urlpatterns = [
    # Becoming / being a partner
    path('apply/', views.apply, name='apply'),
    path('proof/<int:pk>/<slug:kind>/', views.proof_file, name='proof_file'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('offer/<int:pk>/make/', views.make_offer, name='make_offer'),
    path('lead/<int:pk>/pass/', views.pass_lead, name='pass_lead'),
    path('offer/<int:pk>/mark-paid/', views.mark_paid, name='mark_paid'),
    path('analytics/', views.analytics, name='analytics'),
    path('inventory/', views.inventory, name='inventory'),
    path('earnings/', views.earnings, name='earnings'),
    path('earnings/export/', views.export_csv, name='export_csv'),
    path('earnings/settle-all/', views.settle_all, name='settle_all'),
    path('settings/', views.shop_settings, name='settings'),
    path('settings/toggle-open/', views.toggle_accepting, name='toggle_accepting'),
    path('lead/<int:pk>/', views.lead_detail, name='lead_detail'),
    path('buyback-image/<int:pk>/', views.buyback_image, name='buyback_image'),
    path('lead/<int:pk>/suggest/', views.offer_suggestion, name='offer_suggestion'),
    path('commission/<int:pk>/settle/', views.settle_commission, name='settle_commission'),

    # Selling straight to BookBridge
    path('sell/', views.sell_to_bookbridge, name='sell_to_bookbridge'),
    path('sell/check-duplicate/', views.check_duplicate, name='check_duplicate'),
    path('sell/quote/', views.buyback_quote, name='buyback_quote'),
    path('sell/my-requests/', views.buyback_list, name='buyback_list'),
    path('sell/<int:pk>/', views.buyback_detail, name='buyback_detail'),
    path('sell/<int:pk>/accept/', views.accept_offer, name='accept_offer'),
    path('sell/<int:pk>/reject/', views.reject_offer, name='reject_offer'),
    path('sell/<int:pk>/review/', views.review_partner, name='review_partner'),
    path('shop/<int:pk>/', views.partner_profile, name='profile'),
    path('sell/<int:pk>/cancel/', views.cancel_request, name='cancel_request'),

    # Pickup scheduling + handover OTP (seller and assigned partner)
    path('pickup/<int:pk>/propose/', views.propose_pickup, name='propose_pickup'),
    path('pickup/<int:pk>/confirm/', views.confirm_pickup, name='confirm_pickup'),
    path('pickup/<int:pk>/reissue-otp/', views.reissue_pickup_otp, name='reissue_pickup_otp'),
]
