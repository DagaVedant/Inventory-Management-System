from django.conf import settings


def app_flags(request):
    """Expose a couple of deployment facts so templates can be honest.

    Chiefly: only offer password reset when there's a mail server to send it.
    """
    return {
        "email_configured": settings.EMAIL_CONFIGURED,
        "signup_code_required": bool(settings.SIGNUP_CODE),
    }
