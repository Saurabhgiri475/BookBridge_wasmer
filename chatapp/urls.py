from django.urls import path
from . import views

app_name = 'chatapp'

urlpatterns = [
    path('inbox/', views.inbox, name='inbox'),
    path('start/<int:book_pk>/', views.start_conversation, name='start'),
    path('start-buyback/<int:buyback_pk>/', views.start_buyback_conversation, name='start_buyback'),
    path('c/<int:pk>/', views.conversation_view, name='conversation'),
    path('image/<int:pk>/', views.chat_image, name='image'),
]
