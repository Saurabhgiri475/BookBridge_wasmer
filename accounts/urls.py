from django.contrib.auth import views as auth_views
from django.urls import path
from . import views, views_2fa

app_name = 'accounts'

urlpatterns = [
    path('signup/', views.signup, name='signup'),
    path('signup/check/', views.check_availability, name='check_availability'),
    path('signup/verify/', views.verify_signup, name='verify_signup'),
    path('login/', views.BookBridgeLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password-reset/', views.BookBridgePasswordResetView.as_view(), name='password_reset'),
    path('password-reset/sent/', auth_views.PasswordResetDoneView.as_view(
        template_name='accounts/password_reset_done.html'), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', views.BookBridgePasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='accounts/password_reset_complete.html'), name='password_reset_complete'),
    path('profile/', views.profile, name='profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
    path('profile/id/<int:pk>/', views.profile_by_id, name='profile_by_id'),
    path('profile/<str:username>/', views.profile, name='profile_view'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/analytics/', views.seller_analytics, name='seller_analytics'),
    path('verify/', views.seller_verification, name='verify_seller'),
    path('2fa/setup/', views_2fa.setup, name='two_factor_setup'),
    path('2fa/verify/', views_2fa.verify, name='two_factor_verify'),
    path('2fa/', views_2fa.manage, name='two_factor_manage'),
    path('verify/email/send/', views.send_email_otp, name='send_email_otp'),
    path('verify/email/confirm/', views.confirm_email_otp, name='confirm_email_otp'),
]
