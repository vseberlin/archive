from django.contrib import admin
from django.contrib.auth import views as auth
from django.contrib.staticfiles.views import serve
from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='index'),
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
