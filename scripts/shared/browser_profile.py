"""Shared Brazilian desktop-browser fingerprint for municipal web access."""

from __future__ import annotations


CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ACCEPT_LANGUAGE = "pt-BR,pt;q=0.9,en;q=0.8"

REQUEST_HEADERS = {
    "User-Agent": CHROME_USER_AGENT,
    "Accept-Language": ACCEPT_LANGUAGE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}

PLAYWRIGHT_EXTRA_HTTP_HEADERS = {
    "Accept-Language": ACCEPT_LANGUAGE,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

PLAYWRIGHT_CONTEXT_OPTIONS = {
    "locale": "pt-BR",
    "timezone_id": "America/Sao_Paulo",
    "user_agent": CHROME_USER_AGENT,
    "viewport": {"width": 1366, "height": 768},
}

HUMAN_BROWSER_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => false});
Object.defineProperty(navigator, 'language', {get: () => 'pt-BR'});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en']});
"""
