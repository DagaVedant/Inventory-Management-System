from django.conf import settings

SECTIONS = {
    "dashboard": "bench",
    "guide": "guide",
}


def nav_section(request):
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else ""
    if name in SECTIONS:
        return SECTIONS[name]
    if name.startswith("part") or name.startswith("tag") or name.startswith("shopping"):
        return "parts"
    if name.startswith("project") or name.startswith("line"):
        return "projects"
    return ""


def app_flags(request):
    return {
        "signup_code_required": bool(settings.SIGNUP_CODE),
        "nav_section": nav_section(request),
    }
