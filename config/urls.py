from django.contrib import admin
from django.urls import include, path

from core.views import GuardedPasswordResetView, SignupView, ThrottledLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
    path("accounts/login/", ThrottledLoginView.as_view(), name="login"),
    path(
        "accounts/password_reset/",
        GuardedPasswordResetView.as_view(),
        name="password_reset",
    ),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
