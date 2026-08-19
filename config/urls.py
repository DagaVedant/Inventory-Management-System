from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Django's built-in login/logout views, so we don't hand-roll auth.
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
