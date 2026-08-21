"""A small rate limit for the two endpoints worth guessing at.

Signup is open and login accepts anything, so both are worth slowing down. This
is deliberately not a dependency: one cache-backed counter is enough for a
single container, and pulling in a full lockout library for a personal app is
more moving parts than the problem deserves.

The honest limitation: the default cache is per process. Two web workers get a
window each, so the effective limit is roughly the configured one times the
number of workers. That still turns unlimited guessing into slow guessing,
which is the point. A shared cache would make it exact.
"""

from django.core.cache import cache


def client_ip(request):
    """Best guess at who is asking.

    X-Forwarded-For is set by the platform's proxy; its first entry is the
    original client. Trusting it is fine here because nothing reaches the app
    except through that proxy.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def too_many(request, bucket, limit, window_seconds):
    """Count this attempt. True once the limit is used up.

    Only failures should be counted, so a person legitimately logging in
    repeatedly is never locked out.
    """
    key = f"throttle:{bucket}:{client_ip(request)}"
    tally = cache.get(key, 0) + 1
    # Set with the full window each time: a burst extends the cool-off rather
    # than letting the original window expire underneath it.
    cache.set(key, tally, window_seconds)
    return tally > limit


def forget(request, bucket):
    """Clear the count after a success, so one typo costs nothing later."""
    cache.delete(f"throttle:{bucket}:{client_ip(request)}")
