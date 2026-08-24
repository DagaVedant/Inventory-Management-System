from django.core.cache import cache


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def too_many(request, bucket, limit, window_seconds):
    key = f"throttle:{bucket}:{client_ip(request)}"
    tally = cache.get(key, 0) + 1
    cache.set(key, tally, window_seconds)
    return tally > limit


def forget(request, bucket):
    cache.delete(f"throttle:{bucket}:{client_ip(request)}")
