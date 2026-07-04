"""Network hardening helpers for Playwright renders."""
from __future__ import annotations

from urllib.parse import urlparse


_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
_TRACKER_HOST_MARKERS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com/tr",
    "hotjar.com",
    "clarity.ms",
    "scorecardresearch.com",
    "newrelic.com",
    "nr-data.net",
)


def _blocked_host(url: str) -> bool:
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return False
    return any(marker in host for marker in _TRACKER_HOST_MARKERS)


def install_resource_blocking(context) -> None:
    """Block heavy/non-content subresources without changing DOM text inputs."""
    def _route(route, request):
        try:
            if request.resource_type in _BLOCK_RESOURCE_TYPES or _blocked_host(request.url):
                return route.abort()
            return route.continue_()
        except Exception:
            try:
                return route.continue_()
            except Exception:
                return None

    context.route("**/*", _route)


def new_context(browser, **kwargs):
    ctx = browser.new_context(**kwargs)
    install_resource_blocking(ctx)
    return ctx
