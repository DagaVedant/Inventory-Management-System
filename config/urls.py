from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Nothing lives at the root yet, and a 404 there looks like the site is
    # broken. Send people to the app instead.
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
]
