"""Network hardening helpers for Playwright renders."""
from __future__ import annotations

from urllib.parse import urlparse

from browser_profile import (
    HUMAN_BROWSER_INIT_SCRIPT,
    PLAYWRIGHT_CONTEXT_OPTIONS,
    PLAYWRIGHT_EXTRA_HTTP_HEADERS,
)

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
    context_options = dict(PLAYWRIGHT_CONTEXT_OPTIONS)
    extra_headers = dict(PLAYWRIGHT_EXTRA_HTTP_HEADERS)
    extra_headers.update(kwargs.pop("extra_http_headers", {}))
    context_options.update(kwargs)
    context_options["extra_http_headers"] = extra_headers
    ctx = browser.new_context(**context_options)
    ctx.add_init_script(HUMAN_BROWSER_INIT_SCRIPT)
    install_resource_blocking(ctx)
    return ctx
