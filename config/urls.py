from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from core.views import SignupView, password_reset_unavailable

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
]

if not settings.EMAIL_CONFIGURED:
    # Shadows Django's password_reset view, which would otherwise render a
    # "check your inbox" page and post the link to a server log. Must come
    # before the auth include - first match wins.
    urlpatterns.append(
        path(
            "accounts/password_reset/",
            password_reset_unavailable,
            name="password_reset",
        )
    )

urlpatterns += [
    # Django's built-in login/logout/password views, so we don't hand-roll auth.
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
