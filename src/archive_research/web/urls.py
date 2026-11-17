from django.contrib import admin
from django.contrib.auth import views as auth
from django.contrib.staticfiles.views import serve
from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('research/', views.page_search, name='page_search'),
    path('research/packet/', views.research_packet, name='research_packet'),
    path('research/packet/media/<int:index>/', views.packet_media, name='packet_media'),
    path('research/packet/sources/<int:index>/', views.packet_source, name='packet_source'),
    path('research/leads/', views.image_leads, name='image_leads'),
    path('research/leads/<int:index>/<slug:side>/', views.image_lead_media, name='image_lead_media'),
    path('research/pages/<slug:key>/', views.extraction_citation, name='extraction_citation'),
    path('research/media/<slug:key>/', views.extraction_media, name='extraction_media'),
    path('research/sources/<slug:key>/', views.extraction_source, name='extraction_source'),
    path('login/', auth.LoginView.as_view(template_name='research_web/login.html'), name='login'),
    path('logout/', auth.LogoutView.as_view(), name='logout'),
    path('cases/<slug:key>/', views.case, name='case'),
    path('cases/<slug:key>/physical-object/', views.physical_object, name='physical_object'),
    path('cases/<slug:key>/citations/<slug:view_key>/', views.citation, name='citation'),
    path('cases/<slug:key>/review/<slug:assertion_key>/', views.review, name='review'),
    path('rebuild/', views.rebuild_projection, name='rebuild'),
    path('export/', views.export, name='export'),
    path('media/<slug:key>/<slug:view_key>/', views.media, name='media'),
    path('sources/<slug:key>/<slug:view_key>/', views.source, name='source'),
    path('admin/', admin.site.urls),
    # Loopback development only; serves packaged Django admin assets, no sample directory.
    path('static/<path:path>', serve, {'insecure': True}),
]
