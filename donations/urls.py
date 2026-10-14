from django.urls import path

from . import views

app_name = 'donations'

urlpatterns = [
    path('', views.home, name='home'),
    path('new/', views.create, name='create'),
    path('mine/', views.my_donations, name='mine'),
    path('verify/', views.verify, name='verify_form'),
    path('verify/<str:code>/', views.verify, name='verify'),
    path('certificate/<str:code>/pdf/', views.certificate_pdf, name='certificate_pdf'),
    path('staff/', views.staff_queue, name='staff_queue'),
    path('staff/<int:pk>/', views.staff_detail, name='staff_detail'),
    path('staff/<int:pk>/accept/', views.staff_accept, name='staff_accept'),
    path('staff/<int:pk>/reject/', views.staff_reject, name='staff_reject'),
    path('staff/<int:pk>/receive/', views.staff_receive, name='staff_receive'),
    path('staff/certificate/<str:code>/revoke/', views.staff_revoke, name='staff_revoke'),
    path('<int:pk>/', views.detail, name='detail'),
    path('<int:pk>/cancel/', views.cancel, name='cancel'),
]
