from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from core.views import SignupView, ThrottledLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
    path("accounts/login/", ThrottledLoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
]
