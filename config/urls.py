from django.contrib import admin
from django.urls import include, path

from core.views import GuardedPasswordResetView, SignupView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
    # Shadows Django's password_reset view so it can refuse politely when no
    # mail server is configured. Must come before the auth include - first
    # match wins.
    path(
        "accounts/password_reset/",
        GuardedPasswordResetView.as_view(),
        name="password_reset",
    ),
    # Django's built-in login/logout/password views, so we don't hand-roll auth.
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
