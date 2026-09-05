from django.utils.cache import add_never_cache_headers


def no_store_when_logged_in(get_response):
    """Stop the browser showing a stale page from its back/forward cache.

    Every page a logged-in user sees is built from live quantities, so none of
    them should be served from cache. Anonymous pages are left alone.
    """

    def middleware(request):
        response = get_response(request)
        if request.user.is_authenticated and not response.has_header("Cache-Control"):
            add_never_cache_headers(response)
        return response

    return middleware
