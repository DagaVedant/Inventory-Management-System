from django.conf import settings


def app_flags(request):
    return {
        "email_configured": settings.EMAIL_CONFIGURED,
        "signup_code_required": bool(settings.SIGNUP_CODE),
    }
