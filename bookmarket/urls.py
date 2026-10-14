from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.static import serve as serve_media

from . import csp as csp_views
from . import pwa

urlpatterns = [
    path('admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),  # set_language: POST language=en|hi (the nav switcher)
    path('accounts/', include('accounts.urls')),
    path('chat/', include('chatapp.urls')),
    path('orders/', include('orders.urls')),
    path('wallet/', include('wallet.urls')),
    path('notifications/', include('notifications.urls')),
    path('staff/', include('adminpanel.urls')),
    path('partners/', include('partners.urls')),
    path('donate/', include('donations.urls')),
    path('payouts/', include('payouts.urls')),
    path('disputes/', include('disputes.urls')),
    path('csp-report/', csp_views.csp_report, name='csp_report'),
    path('sw.js', pwa.service_worker, name='service_worker'),
    path('manifest.webmanifest', pwa.manifest, name='manifest'),
    path('offline/', pwa.offline, name='offline'),
    path('', include('books.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT or settings.BASE_DIR / 'static')

elif not settings.USE_S3_PUBLIC:
    # Local-disk uploads (e.g. a mounted volume) must still be reachable when DEBUG is off.
    # Only the PUBLIC media folder is mapped; PRIVATE_MEDIA_ROOT is never exposed here.
    urlpatterns += [re_path(r'^media/(?P<path>.*)$', serve_media, {'document_root': settings.MEDIA_ROOT})]
