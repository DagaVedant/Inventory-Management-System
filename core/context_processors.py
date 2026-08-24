from django.conf import settings


def app_flags(request):
    return {
        "signup_code_required": bool(settings.SIGNUP_CODE),
    }
