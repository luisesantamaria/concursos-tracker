"""Fail-closed proposal adapter for the two observed Atende.Net surfaces.

This module never adjudicates a candidate.  It turns already captured public
page/XHR/iframe states into item-positive proposals for the existing V2 runner.
Network access and browser ownership deliberately remain outside this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit


_ATENDE_PATHS = ("/cidadao/pagina/", "/autoatendimento/servicos/")
_PLACEHOLDERS = re.compile(
    r"por favor,?\s*aguarde|carregando|loading|em constru[cç][aã]o", re.I
)
_ITEM_WORDS = re.compile(
    r"\b(?:edital|concurso\s+p[uú]blico|processo\s+seletivo|pss)\b", re.I
)
_ITEM_MARKER = re.compile(
    r"(?:\b\d{1,4}\s*/\s*20\d{2}\b|\b20\d{2}\b|\b\d{1,2}[./-]\d{1,2}[./-]20\d{2}\b)",
    re.I,
)
_DOCUMENT_HINT = re.compile(r"(?:\.pdf(?:$|[?#])|download|arquivo|documento)", re.I)
_GENERIC_LABELS = {
    "concurso",
    "concursos",
    "concursos publicos",
    "processo seletivo",
    "processos seletivos",
    "editais",
    "arquivos",
    "acessar",
}
_TITLE_KEYS = ("titulo", "title", "nome", "descricao", "description", "texto", "text")
_URL_KEYS = ("href", "url", "link", "arquivo", "documento", "download")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    return "".join(c for c in normalized if not unicodedata.combining(c)).casefold()


def _safe_public_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not (
        parsed.username or parsed.password
    )


def _is_atende_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname is not None and parsed.hostname.lower().endswith(".atende.net")


def detect_atende_shell(page_url: str, html: str) -> bool:
    """Detect an Atende shell from a provider URL plus independent page signals."""

    if not _is_atende_url(page_url) or not any(p in urlsplit(page_url).path for p in _ATENDE_PATHS):
        return False
    folded = _fold(html)
    provider_signal = any(
        signal in folded
        for signal in ("atende.net", "ipm sistemas", "portal do cidadao", "autoatendimento")
    )
    component_signal = "pluginportal" in folded
    shell_signal = "por favor, aguarde" in folded
    municipal_signal = "municipio de" in folded or "prefeitura municipal" in folded
    return provider_signal and municipal_signal and (component_signal or shell_signal)


def derive_service_detail_url(service_url: str) -> str:
    """Derive ``/detalhar/1`` from any Atende service slug without host knowledge."""

    parsed = urlsplit(service_url)
    if not _is_atende_url(service_url) or not _safe_public_url(service_url):
        raise ValueError("service_url must be a public *.atende.net URL")
    match = re.fullmatch(r"(/autoatendimento/servicos/[^/]+?)(?:/detalhar/1)?/?", parsed.path)
    if match is None:
        raise ValueError("service_url must identify one /autoatendimento/servicos/<slug>")
    return urlunsplit((parsed.scheme, parsed.netloc, match.group(1) + "/detalhar/1", parsed.query, ""))


@dataclass(frozen=True)
class AtendeItem:
    title: str
    document_url: str
    row_text: str
    response_url: str
    response_sha256: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AtendeProposal:
    """Uniform proposal consumed later by an authority/adjudication layer."""

    url_candidata: str
    mode: str
    status: str
    evidence_state: str
    evidence: tuple[AtendeItem, ...]
    provenance: tuple[Mapping[str, str], ...]
    confirms: bool = False

    @property
    def decision(self) -> str:
        return "proponer"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["decision"] = self.decision
        return value


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href = ""
        self._parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((_text(" ".join(self._parts)), self._href))
            self._href = ""
            self._parts = []


def _mapping_value(row: Mapping[str, Any], keys: Sequence[str]) -> str:
    folded = {str(key).casefold(): value for key, value in row.items()}
    for key in keys:
        value = folded.get(key)
        if isinstance(value, (str, int)) and _text(value):
            return _text(value)
    return ""


def _candidate_rows(value: Any) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if isinstance(value, Mapping):
        title = _mapping_value(value, _TITLE_KEYS)
        href = _mapping_value(value, _URL_KEYS)
        if title and href:
            found.append((title, href, _text(value.get("row_text") or title)))
        for child in value.values():
            found.extend(_candidate_rows(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_candidate_rows(child))
    elif isinstance(value, str) and "<a" in value.casefold():
        parser = _LinkParser()
        parser.feed(value)
        found.extend((title, href, title) for title, href in parser.links)
    return found


def _item_positive(title: str, href: str) -> bool:
    folded = _fold(title)
    if not title or not href or _PLACEHOLDERS.search(title) or folded in _GENERIC_LABELS:
        return False
    return bool(
        _ITEM_WORDS.search(title)
        and (_ITEM_MARKER.search(title) or _DOCUMENT_HINT.search(href))
        and (_DOCUMENT_HINT.search(href) or urlsplit(href).path)
    )


def parse_plugin_portal_response(
    payload: str | bytes | Mapping[str, Any] | list[Any],
    *,
    response_url: str,
) -> tuple[AtendeItem, ...]:
    """Parse a captured PluginPortal JSON or HTML response, never a shell."""

    if not _safe_public_url(response_url):
        raise ValueError("response_url must be public HTTP(S)")
    raw = payload if isinstance(payload, bytes) else (
        payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    body_hash = sha256(raw).hexdigest()
    if isinstance(payload, bytes):
        decoded: Any = payload.decode("utf-8")
    else:
        decoded = payload
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            pass

    items: list[AtendeItem] = []
    seen: set[tuple[str, str]] = set()
    for title, href, row_text in _candidate_rows(decoded):
        absolute = urljoin(response_url, href)
        key = (_fold(title), absolute)
        if key in seen or not _safe_public_url(absolute) or not _item_positive(title, absolute):
            continue
        seen.add(key)
        items.append(AtendeItem(_text(title), absolute, _text(row_text), response_url, body_hash))
    return tuple(items)


def _materialized(item: AtendeItem, rendered_html: str) -> bool:
    folded_render = _fold(rendered_html)
    return _fold(item.title) in folded_render and (
        item.document_url in rendered_html
        or urlsplit(item.document_url).path in rendered_html
    )


def _proposal(
    *, canonical_url: str, mode: str, items: Sequence[AtendeItem], provenance: Sequence[Mapping[str, str]]
) -> list[dict[str, Any]]:
    states = [dict(state) for state in provenance]
    return [
        {
            "title": item.title,
            "document_url": item.document_url,
            "url_candidata": canonical_url,
            "source_url": canonical_url,
            "mode": mode,
            "disposition": "propose",
            "confirmed": False,
            "provenance": states,
            "evidence": {
                "item_positive": True,
                "evidence_state": "item_positive",
                "row_text": item.row_text,
                "document_url": item.document_url,
                "response_url": item.response_url,
                "raw_response_sha256": item.response_sha256,
                "states": states,
            },
        }
        for item in items
    ]


def propose_candidates(
    page_url: str,
    *,
    page_html: str,
    plugin_response: str | bytes | Mapping[str, Any] | list[Any] | None = None,
    plugin_response_url: str = "",
    rendered_html: str = "",
    iframe_capture: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Propose a canonical Atende surface only from materialized real items.

    ``plugin_response`` and ``iframe_capture`` are inputs captured by the
    existing runner in one fresh public session.  This function does no I/O.
    """

    if not detect_atende_shell(page_url, page_html):
        return []
    path = urlsplit(page_url).path
    if "/cidadao/pagina/" in path:
        if plugin_response is None or not plugin_response_url or not rendered_html:
            return []
        if urlsplit(plugin_response_url).netloc.casefold() != urlsplit(page_url).netloc.casefold():
            return []
        # The runner supplies the matched component's outerHTML, not the whole
        # page.  Requiring its provider marker prevents a navigation/sidebar
        # duplicate from satisfying the response-to-DOM join.
        if "pluginportal" not in _fold(rendered_html):
            return []
        items = parse_plugin_portal_response(plugin_response, response_url=plugin_response_url)
        materialized = tuple(item for item in items if _materialized(item, rendered_html))
        return _proposal(
            canonical_url=page_url,
            mode="pagina_plugin_portal",
            items=materialized,
            provenance=(
                {"state": "shell", "url": page_url, "evidence": "atende_shell"},
                {"state": "xhr_response", "url": plugin_response_url, "evidence": "captured_public_response"},
                {"state": "rendered", "url": page_url, "evidence": "response_items_materialized_in_plugin_container"},
            ),
        )

    if "/autoatendimento/servicos/" in path:
        if not iframe_capture:
            return []
        detail_url = derive_service_detail_url(page_url)
        if _text(iframe_capture.get("detail_url")) != detail_url:
            return []
        iframe_src = _text(iframe_capture.get("iframe_src"))
        response_url = _text(iframe_capture.get("response_url") or iframe_src)
        frame_html = _text(iframe_capture.get("rendered_html"))
        rows = iframe_capture.get("rows")
        if not iframe_src or not _safe_public_url(iframe_src) or not response_url or not frame_html or rows is None:
            return []
        items = parse_plugin_portal_response(rows, response_url=response_url)
        materialized = tuple(item for item in items if _materialized(item, frame_html))
        return _proposal(
            canonical_url=page_url,
            mode="servicio_iframe",
            items=materialized,
            provenance=(
                {"state": "service", "url": page_url, "evidence": "atende_service_shell"},
                {"state": "detail", "url": detail_url, "evidence": "derived_detail_navigation"},
                {"state": "iframe", "url": iframe_src, "evidence": "public_child_frame"},
                {"state": "frame_response", "url": response_url, "evidence": "item_rows_materialized_in_frame"},
            ),
        )
    return []


def plan_playwright(page_url: str, *, page_html: str) -> Mapping[str, Any] | None:
    """Return an exact runner plan; it intentionally contains no guessed XHR URL."""

    if not detect_atende_shell(page_url, page_html):
        return None
    common: dict[str, Any] = {
        "mode": "plan_playwright",
        "canonical_url": page_url,
        "session": {
            "new_public_context": True,
            "import_storage_state": False,
            "persist_cookie_values": False,
            "abort_on": ["login", "captcha", "antibot"],
        },
        "navigation": {"wait_until": "domcontentloaded", "timeout_ms": 30000},
        "network_capture": {
            "install_before_goto": True,
            "resource_types": ["xhr", "fetch"],
            "request_fields": ["url", "method", "header_names", "sanitized_post_data"],
            "response_fields": ["url", "status", "content_type", "body", "sha256"],
            "forbidden_values": ["cookie", "authorization", "antiforgery_token"],
        },
    }
    if "/cidadao/pagina/" in urlsplit(page_url).path:
        common.update({
            "surface": "pagina_plugin_portal",
            "component_match": "[id*='PluginPortal' i], [class*='PluginPortal' i], [data-plugin*='PluginPortal' i], [data-component*='PluginPortal' i], [data-componente*='PluginPortal' i]",
            "request_contract": "observe method/url/params from xhr|fetch initiated after goto and associated with the matched component; do not reuse a plugin id or endpoint",
            "waits": [
                {"for": "plugin_component_attached", "timeout_ms": 20000},
                {"for": "real_item_link_inside_component", "timeout_ms": 20000},
                {"for": "quiet_after_first_item", "timeout_ms": 2000},
            ],
            "accept_only_if": "response title+href also occur inside the rendered plugin component",
        })
    else:
        common.update({
            "surface": "servicio_iframe",
            "detail_url": derive_service_detail_url(page_url),
            "request_contract": "capture main-frame requests until iframe creation, then requests/responses initiated by that child frame",
            "waits": [
                {"for": "iframe[src]_attached", "timeout_ms": 20000},
                {"for": "iframe_domcontentloaded", "timeout_ms": 20000},
                {"for": "real_item_link_inside_iframe", "timeout_ms": 20000},
                {"for": "quiet_after_first_item", "timeout_ms": 2000},
            ],
            "accept_only_if": "a repeated frame row/card has item title and document link and is traceable to a captured public frame response",
        })
    return common


__all__ = [
    "AtendeItem",
    "AtendeProposal",
    "derive_service_detail_url",
    "detect_atende_shell",
    "parse_plugin_portal_response",
    "plan_playwright",
    "propose_candidates",
]
