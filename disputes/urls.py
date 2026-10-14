from django.urls import path
from . import views

app_name = 'disputes'

urlpatterns = [
    # Buyer / seller
    path('', views.my_disputes, name='mine'),
    path('new/<uuid:order_id>/', views.open_dispute, name='open'),
    path('<int:pk>/', views.dispute_detail, name='detail'),
    path('<int:pk>/message/', views.post_message, name='message'),
    path('<int:pk>/agree/', views.seller_agree, name='seller_agree'),
    path('<int:pk>/escalate/', views.escalate, name='escalate'),
    path('<int:pk>/withdraw/', views.withdraw, name='withdraw'),
    path('evidence/<int:pk>/', views.evidence_file, name='evidence'),
    # Staff
    path('staff/', views.staff_queue, name='staff_queue'),
    path('staff/<int:pk>/', views.staff_detail, name='staff_detail'),
    path('staff/<int:pk>/message/', views.staff_message, name='staff_message'),
    path('staff/<int:pk>/note/', views.staff_note, name='staff_note'),
    path('staff/<int:pk>/resolve/', views.staff_resolve, name='staff_resolve'),
]
