"""Fail-closed proposal adapter for public server-side DataTables listings.

The adapter deliberately stops at proposals.  Authority adjudication and final
confirmation belong to the integrating runner, not to this module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
OBSERVED_PAGING = (("draw", "1"), ("start", "0"), ("length", "100"))


class DataTablesAdapterError(ValueError):
    """The observed surface does not satisfy the adapter's safety contract."""


@dataclass(frozen=True)
class DataTablesDetection:
    endpoint: str
    columns: tuple[str, ...]
    signals: tuple[str, ...]


class _TextAndLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append(
                {"title": _clean_space(" ".join(self._link_text)), "url": self._href}
            )
            self._href = None
            self._link_text = []


def _clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _plain_html(value: Any) -> tuple[str, list[dict[str, str]]]:
    text = "" if value is None else str(value)
    parser = _TextAndLinks()
    parser.feed(text)
    parser.close()
    return _clean_space(" ".join(parser.text)), parser.links


def _balanced_object(source: str, opening: int) -> str | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening : index + 1]
    return None


def _initializer_objects(page_html: str) -> list[str]:
    objects: list[str] = []
    pattern = re.compile(r"\.(?:DataTable|dataTable)\s*\(\s*\{", re.IGNORECASE)
    for match in pattern.finditer(page_html):
        opening = page_html.find("{", match.start())
        block = _balanced_object(page_html, opening)
        if block:
            objects.append(block)
    return objects


def _quoted_property(block: str, property_name: str) -> str | None:
    match = re.search(
        rf"(?:^|[,{{])\s*['\"]?{re.escape(property_name)}['\"]?\s*:\s*(['\"])(.*?)\1",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    return unescape(match.group(2).strip()) if match else None


def _ajax_endpoint(block: str, page_url: str) -> str | None:
    ajax_object = re.search(r"(?:^|[,{{])\s*ajax\s*:\s*\{", block, re.IGNORECASE)
    if ajax_object:
        opening = block.find("{", ajax_object.start())
        ajax_block = _balanced_object(block, opening)
        if not ajax_block:
            return None
        method = _quoted_property(ajax_block, "method") or _quoted_property(ajax_block, "type")
        if method and method.upper() != "GET":
            return None
        endpoint = _quoted_property(ajax_block, "url")
        return urljoin(page_url, endpoint) if endpoint else None

    direct = re.search(
        r"(?:^|[,{{])\s*ajax\s*:\s*(['\"])(.*?)\1",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if direct:
        return urljoin(page_url, unescape(direct.group(2).strip()))
    if re.search(r"(?:^|[,{{])\s*ajax\s*:\s*(?:window\.)?location(?:\.href)?\b", block, re.I):
        return page_url
    return None


def _columns(block: str) -> tuple[str, ...]:
    columns_match = re.search(r"(?:^|[,{{])\s*columns\s*:\s*\[", block, re.IGNORECASE)
    if not columns_match:
        return ()
    start = block.find("[", columns_match.start())
    depth = 0
    quote: str | None = None
    escaped = False
    end = len(block)
    for index in range(start, len(block)):
        char = block[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    columns_block = block[start:end]
    return tuple(
        match.group(2).strip()
        for match in re.finditer(
            r"\bdata\s*:\s*(['\"])(.*?)\1", columns_block, re.IGNORECASE | re.DOTALL
        )
    )


def detect_datatables_server_side(page_html: str, url: str) -> DataTablesDetection | None:
    """Return convergent server-side DataTables evidence, otherwise ``None``."""
    if not isinstance(page_html, str) or not page_html.strip():
        return None
    for block in _initializer_objects(page_html):
        if not re.search(r"\bserverSide\s*:\s*true\b", block, re.IGNORECASE):
            continue
        endpoint = _ajax_endpoint(block, url)
        if not endpoint:
            continue
        signals = ["datatable_initialization", "server_side_true", "ajax_endpoint_observed"]
        if re.search(r"\bprocessing\s*:\s*true\b", block, re.IGNORECASE):
            signals.append("processing_true")
        if re.search(r"(?:jquery\.)?dataTables(?:\.min)?\.(?:js|css)|dataTables\.bootstrap", page_html, re.I):
            signals.append("datatables_asset")
        columns = _columns(block)
        if columns:
            signals.append("columns_mapping")
        return DataTablesDetection(endpoint=endpoint, columns=columns, signals=tuple(signals))
    return None


def _validate_delegation(page_url: str, delegation_proof: str | None) -> str:
    if not delegation_proof:
        raise DataTablesAdapterError(
            "delegation_proof is required: provide the official municipal origin URL "
            "that delegates to this listing"
        )
    proof = urlsplit(delegation_proof)
    page = urlsplit(page_url)
    if proof.scheme not in {"http", "https"} or not proof.hostname:
        raise DataTablesAdapterError("delegation_proof must be an absolute public HTTP(S) URL")
    if page.scheme not in {"http", "https"} or not page.hostname:
        raise DataTablesAdapterError("url must be an absolute public HTTP(S) URL")
    if proof.hostname.lower() == page.hostname.lower():
        raise DataTablesAdapterError(
            "delegation_proof must identify the distinct official origin, not the delegated host"
        )
    return delegation_proof


def _observed_tipo(page_url: str) -> str:
    values = [value for key, value in parse_qsl(urlsplit(page_url).query, keep_blank_values=True) if key == "tipo"]
    if len(values) != 1 or not values[0].strip():
        raise DataTablesAdapterError("url must contain exactly one non-empty observed 'tipo' parameter")
    return values[0]


def _request_url(endpoint: str, page_url: str, observed_tipo: str) -> str:
    endpoint_parts = urlsplit(endpoint)
    page_parts = urlsplit(page_url)
    if endpoint_parts.hostname != page_parts.hostname:
        raise DataTablesAdapterError("observed AJAX endpoint must remain on the listing host")
    pairs = parse_qsl(endpoint_parts.query, keep_blank_values=True)
    endpoint_tipos = [value for key, value in pairs if key == "tipo"]
    if endpoint_tipos and endpoint_tipos != [observed_tipo]:
        raise DataTablesAdapterError("AJAX endpoint changes the observed 'tipo' filter")
    if not endpoint_tipos:
        pairs.append(("tipo", observed_tipo))
    for key, value in OBSERVED_PAGING:
        current = [item for name, item in pairs if name == key]
        if current and current != [value]:
            raise DataTablesAdapterError(f"AJAX endpoint changes observed DataTables parameter {key}")
        if not current:
            pairs.append((key, value))
    return urlunsplit((endpoint_parts.scheme, endpoint_parts.netloc, endpoint_parts.path, urlencode(pairs), ""))


def _response_bytes(response: Any) -> bytes:
    status = getattr(response, "status_code", 200)
    if status != 200:
        raise DataTablesAdapterError(f"DataTables request returned HTTP {status}, expected 200")
    if isinstance(response, bytes):
        return response
    if isinstance(response, str):
        return response.encode("utf-8")
    if isinstance(response, Mapping):
        return json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise DataTablesAdapterError("fetcher must return bytes, str, a mapping, or a response with content")


def _validated_payload(raw: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataTablesAdapterError("DataTables response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise DataTablesAdapterError("DataTables response must be a JSON object")
    for field in ("draw", "recordsTotal", "recordsFiltered"):
        if not isinstance(payload.get(field), int) or isinstance(payload.get(field), bool) or payload[field] < 0:
            raise DataTablesAdapterError(f"DataTables field {field} must be a non-negative integer")
    if payload["draw"] != 1:
        raise DataTablesAdapterError("DataTables response draw does not match observed draw=1")
    if payload["recordsFiltered"] > payload["recordsTotal"]:
        raise DataTablesAdapterError("recordsFiltered cannot exceed recordsTotal")
    if not isinstance(payload.get("data"), list) or len(payload["data"]) > 100:
        raise DataTablesAdapterError("DataTables data must be a list with at most length=100 rows")
    return payload


def _row_mapping(row: Any, columns: tuple[str, ...]) -> Mapping[str, Any] | None:
    if isinstance(row, Mapping):
        return row
    if isinstance(row, list) and columns and len(row) == len(columns):
        return dict(zip(columns, row, strict=True))
    return None


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _fold(value)).strip("_")


def _first(mapping: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    keyed = {_key(str(key)): value for key, value in mapping.items()}
    for alias in aliases:
        if alias in keyed and keyed[alias] not in (None, "", []):
            return keyed[alias]
    return None


def _attachments(mapping: Mapping[str, Any], page_url: str) -> list[dict[str, str]]:
    attachment_keys = {"anexo", "anexos", "arquivo", "arquivos", "attachment", "attachments", "documento", "documentos"}
    found: list[dict[str, str]] = []
    for key, value in mapping.items():
        plain, links = _plain_html(value)
        for link in links:
            found.append({"title": link["title"] or plain, "url": urljoin(page_url, link["url"])})
        if _key(str(key)) in attachment_keys and plain and not links:
            found.append({"title": plain, "url": ""})
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for attachment in found:
        identity = (attachment["title"], attachment["url"])
        if identity not in seen:
            seen.add(identity)
            unique.append(attachment)
    return unique


def _number_year(mapping: Mapping[str, Any], title: str) -> tuple[str, str]:
    number_value = _first(mapping, ("numero_processo", "numero", "n", "process_number"))
    year_value = _first(mapping, ("ano_processo", "ano", "year", "process_year"))
    number = _plain_html(number_value)[0] if number_value is not None else ""
    year = _plain_html(year_value)[0] if year_value is not None else ""
    combined = re.search(r"\b(\d{1,6})\s*[/.-]\s*((?:19|20)\d{2})\b", number)
    if combined:
        return combined.group(1), combined.group(2)
    title_match = re.search(r"\b(\d{1,6})\s*[/.-]\s*((?:19|20)\d{2})\b", title)
    if title_match:
        number = number or title_match.group(1)
        year = year or title_match.group(2)
    return number, year


def _item_positive(mapping: Mapping[str, Any], observed_tipo: str) -> tuple[bool, str]:
    parts = [_plain_html(value)[0] for value in mapping.values() if not isinstance(value, (dict, list))]
    evidence = _clean_space(" ".join(part for part in parts if part))
    folded = _fold(evidence)
    excluded = (
        "concurso de soberanas",
        "concurso cultural",
        "licitacao",
        "pregao",
        "chamamento publico",
    )
    if any(term in folded for term in excluded):
        return False, evidence
    tokens = [token for token in re.split(r"[^a-z0-9]+", _fold(observed_tipo)) if token]
    return bool(tokens) and all(token in folded for token in tokens), evidence


def _normalize_row(mapping: Mapping[str, Any], page_url: str) -> dict[str, Any]:
    title_value = _first(mapping, ("titulo", "title", "nome", "descricao_simples"))
    date_value = _first(
        mapping,
        ("data_publicacao", "data_inicio", "data", "date", "publicado_em", "created_at"),
    )
    title = _plain_html(title_value)[0]
    date = _plain_html(date_value)[0]
    number, year = _number_year(mapping, title)
    return {
        "title": title,
        "date": date,
        "attachments": _attachments(mapping, page_url),
        "process_number": number,
        "process_year": year,
    }


def propose_candidates(
    page_html: str,
    url: str,
    delegation_proof: str | None,
    fetcher: Callable[..., Any],
) -> list[dict[str, Any]]:
    """Propose item-positive rows; never confirm or adjudicate them.

    ``fetcher`` is called once as ``fetcher(request_url, headers=XHR_HEADERS)``.
    The request URL contains only the observed ``tipo`` plus the frozen observed
    DataTables query ``draw=1&start=0&length=100``.
    """
    proof = _validate_delegation(url, delegation_proof)
    observed_tipo = _observed_tipo(url)
    detection = detect_datatables_server_side(page_html, url)
    if detection is None:
        return []
    request_url = _request_url(detection.endpoint, url, observed_tipo)
    raw = _response_bytes(fetcher(request_url, headers=dict(XHR_HEADERS)))
    payload = _validated_payload(raw)
    raw_hash = sha256(raw).hexdigest()
    raw_text = raw.decode("utf-8-sig")

    proposals: list[dict[str, Any]] = []
    for row in payload["data"]:
        mapping = _row_mapping(row, detection.columns)
        if mapping is None:
            continue
        positive, quote = _item_positive(mapping, observed_tipo)
        if not positive:
            continue
        normalized = _normalize_row(mapping, url)
        if not normalized["title"]:
            continue
        proposals.append(
            {
                **normalized,
                "disposition": "propose",
                "confirmed": False,
                "source_url": url,
                "request_url": request_url,
                "observed_tipo": observed_tipo,
                "delegation_proof": proof,
                "evidence": {
                    "item_positive": True,
                    "quote": quote,
                    "detection_signals": list(detection.signals),
                    "raw_response_json": raw_text,
                    "raw_response_sha256": raw_hash,
                },
            }
        )
    return proposals


__all__ = [
    "DataTablesAdapterError",
    "DataTablesDetection",
    "detect_datatables_server_side",
    "propose_candidates",
]
