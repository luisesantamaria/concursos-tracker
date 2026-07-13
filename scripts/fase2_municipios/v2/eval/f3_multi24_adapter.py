"""Pure, fail-closed Multi24 tree analyser for the conditional F3 mini-wave.

The module performs no I/O.  Callers provide immutable HTTP snapshots and the
pages reached by links that were actually present in the entry snapshot.  The
adapter never guesses a Multi24 ``id`` and never returns a confirmation: its
strongest disposition is ``candidata`` for a later deterministic gate.

The raw bytes are hashed before decoding.  This matters for Multi24 because
real captures disagree about charset metadata: some bodies are genuine
Latin-1 with a UTF-8 ``meta`` tag, while other installations serve valid UTF-8
under an ISO-8859-1 HTTP declaration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag


VALID_BUCKETS = frozenset({"concurso_publico", "processo_seletivo"})
VALID_DISPOSITIONS = frozenset({"candidata", "revisar"})
MIN_ITEM_EVIDENCE = 2

_MULTI24_PATH_RE = re.compile(r"^/multi24/sistemas/transparencia(?:/index)?/?$", re.IGNORECASE)
_EXCLUDED_BRANCH_PATTERNS = (
    re.compile(r"\bchamadas?\s+publicas?\b"),
    re.compile(r"\bconcursos?\s+culturais?\b"),
    re.compile(r"\blicitac(?:ao|oes)\b"),
    re.compile(r"\bsoberanas?\b"),
    re.compile(r"\brainhas?\b"),
    re.compile(r"\bnoticias?\b"),
)
_ITEM_TERM_PATTERNS = (
    ("edital", re.compile(r"\b(?:edital|editais)\b")),
    ("retificacao", re.compile(r"\bretificac(?:ao|oes)\b")),
    ("homologacao", re.compile(r"\bhomologac(?:ao|oes)\b")),
    ("gabarito", re.compile(r"\bgabaritos?\b")),
    ("resultado", re.compile(r"\bresultados?\b")),
    ("classificacao", re.compile(r"\bclassificac(?:ao|oes)\b")),
    ("convocacao", re.compile(r"\bconvocac(?:ao|oes)\b")),
    ("inscricao", re.compile(r"\binscric(?:ao|oes)\b")),
    ("prova", re.compile(r"\bprovas?\b")),
    ("lista_final", re.compile(r"\blistas?\s+finais?\b")),
)
_SOFT_404_TERMS = (
    "pagina nao encontrada",
    "conteudo solicitado nao esta disponivel",
    "erro 404",
    "pagina inexistente",
)


class Multi24ContractError(ValueError):
    """Raised for an invalid caller contract, never for missing evidence."""


@dataclass(frozen=True)
class Multi24Snapshot:
    """An immutable HTTP snapshot supplied by the acquisition layer."""

    requested_url: str
    final_url: str
    status_code: int
    body: bytes
    content_type: str = ""
    retrieved_at: str = ""


@dataclass(frozen=True)
class Multi24Authority:
    """Pre-validated authority supplied by the deterministic upstream gate.

    ``official_source_origins`` are pre-approved municipal origins from the
    deterministic authority map.  Each supplied snapshot must originate there
    and contain a real ``a[href]`` to the analysed portal origin.  Thus the
    parser can verify the navigation chain without network access.
    """

    official_source_origins: tuple[str, ...]
    navigation_snapshots: tuple[Multi24Snapshot, ...]


@dataclass(frozen=True)
class Multi24DecodeInfo:
    """Auditable result of decoding one raw snapshot."""

    text: str
    raw_sha256: str
    charset_used: str
    declared_charset: str
    meta_charset: str
    header_mismatch: bool
    meta_mismatch: bool
    mojibake_repaired: bool


@dataclass(frozen=True)
class Multi24Edge:
    """A navigation edge extracted from an actual ``a[href]`` element."""

    source_url: str
    target_url: str
    label: str
    provenance: tuple[str, ...]
    depth: int


@dataclass(frozen=True)
class Multi24Item:
    """Secondary item evidence found in the linked HTML node."""

    title: str
    url: str


@dataclass(frozen=True)
class Multi24Candidate:
    """A current-year node that has passed the adapter's offline contract."""

    node_url: str
    label: str
    bucket: str
    provenance: tuple[str, ...]
    items: tuple[Multi24Item, ...]


@dataclass(frozen=True)
class Multi24Result:
    """Fail-closed analysis result; this type deliberately has no confirmation."""

    disposition: str
    reason: str
    review_reasons: tuple[str, ...]
    municipio: str
    bucket: str
    current_year: int
    index_url: str
    edges: tuple[Multi24Edge, ...]
    candidates: tuple[Multi24Candidate, ...]
    authority_evidence: tuple[str, ...]
    identity_evidence: tuple[str, ...]
    platform_evidence: tuple[str, ...]
    raw_sha256_by_url: tuple[tuple[str, str], ...]
    charset_by_url: tuple[tuple[str, str], ...]
    header_mismatch_urls: tuple[str, ...]
    meta_mismatch_urls: tuple[str, ...]
    mojibake_repaired_urls: tuple[str, ...]

    @property
    def active_node_urls(self) -> tuple[str, ...]:
        return tuple(candidate.node_url for candidate in self.candidates)

    @property
    def items(self) -> tuple[Multi24Item, ...]:
        seen: set[tuple[str, str]] = set()
        flattened: list[Multi24Item] = []
        for candidate in self.candidates:
            for item in candidate.items:
                key = (item.title, item.url)
                if key not in seen:
                    seen.add(key)
                    flattened.append(item)
        return tuple(flattened)


@dataclass(frozen=True)
class _ParsedPage:
    snapshot: Multi24Snapshot
    url: str
    decoded: Multi24DecodeInfo
    soup: BeautifulSoup
    visible_text: str
    normalized_text: str


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_marks).strip().casefold()


def _canonical_charset(value: str) -> str:
    token = value.strip().strip("\"'").casefold().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf-8": "utf-8",
        "latin1": "iso-8859-1",
        "latin-1": "iso-8859-1",
        "iso8859-1": "iso-8859-1",
        "iso-8859-1": "iso-8859-1",
        "windows-1252": "windows-1252",
        "cp1252": "windows-1252",
    }
    return aliases.get(token, token)


def _declared_charset(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*([^;\s]+)", content_type, flags=re.IGNORECASE)
    return _canonical_charset(match.group(1)) if match else ""


def _meta_charset(raw: bytes) -> str:
    head = raw[:8192].decode("ascii", errors="ignore")
    match = re.search(
        r"<meta[^>]+charset\s*=\s*[\"']?([^\s\"'/>;]+)",
        head,
        flags=re.IGNORECASE,
    )
    return _canonical_charset(match.group(1)) if match else ""


def _mojibake_marker_count(value: str) -> int:
    return sum(value.count(marker) for marker in ("Ã", "Â", "â€", "â€™", "â€“", "â€”"))


def _repair_mojibake_once(value: str) -> tuple[str, bool]:
    """Apply at most one reversible legacy-codec -> UTF-8 repair when useful."""

    before = _mojibake_marker_count(value)
    if before == 0:
        return value, False
    best = value
    best_count = before
    for codec in ("iso-8859-1", "windows-1252"):
        try:
            repaired = value.encode(codec, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        marker_count = _mojibake_marker_count(repaired)
        if marker_count < best_count:
            best = repaired
            best_count = marker_count
    return (best, True) if best_count < before else (value, False)


def decode_snapshot(snapshot: Multi24Snapshot) -> Multi24DecodeInfo:
    """Decode bytes once while preserving raw hash and metadata disagreement."""

    if not isinstance(snapshot, Multi24Snapshot):
        raise Multi24ContractError("snapshot_must_be_multi24_snapshot")
    if not isinstance(snapshot.body, bytes):
        raise Multi24ContractError("snapshot_body_must_be_bytes")

    raw = snapshot.body
    digest = hashlib.sha256(raw).hexdigest()
    declared = _declared_charset(snapshot.content_type)
    meta = _meta_charset(raw)

    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
        used = "utf-8"
    else:
        try:
            text = raw.decode("utf-8", errors="strict")
            used = "utf-8"
        except UnicodeDecodeError:
            fallbacks: list[str] = []
            if declared in {"iso-8859-1", "windows-1252"}:
                fallbacks.append(declared)
            elif declared and declared != "utf-8":
                fallbacks.append(declared)
            fallbacks.extend(["windows-1252", "iso-8859-1"])

            text = ""
            used = ""
            for charset in dict.fromkeys(fallbacks):
                try:
                    text = raw.decode(charset, errors="strict")
                    used = _canonical_charset(charset)
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            if not used:  # Latin-1 maps every byte, so this is defensive only.
                text = raw.decode("iso-8859-1")
                used = "iso-8859-1"

    text, mojibake_repaired = _repair_mojibake_once(text)
    return Multi24DecodeInfo(
        text=text,
        raw_sha256=digest,
        charset_used=used,
        declared_charset=declared,
        meta_charset=meta,
        header_mismatch=bool(declared and declared != used),
        meta_mismatch=bool(meta and meta != used),
        mojibake_repaired=mojibake_repaired,
    )


def _snapshot_url(snapshot: Multi24Snapshot) -> str:
    return snapshot.final_url.strip() or snapshot.requested_url.strip()


def _parse_snapshot(snapshot: Multi24Snapshot) -> _ParsedPage:
    decoded = decode_snapshot(snapshot)
    soup = BeautifulSoup(decoded.text, "html.parser")
    visible = soup.get_text(" ", strip=True)
    return _ParsedPage(
        snapshot=snapshot,
        url=_snapshot_url(snapshot),
        decoded=decoded,
        soup=soup,
        visible_text=visible,
        normalized_text=_norm(visible),
    )


def _query_values(url: str) -> dict[str, list[str]]:
    return {key.casefold(): values for key, values in parse_qs(urlsplit(url).query).items()}


def _origin(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.casefold() != "https" or not host or parsed.username or parsed.password:
        return ""
    default_port = 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"https://{host}{suffix}"


def _is_multi24_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if not _MULTI24_PATH_RE.search(parsed.path):
        return False
    query = _query_values(url)
    return (
        any(value.casefold() == "dinamico" for value in query.get("secao", []))
        and any(value.strip() for value in query.get("id", []))
    )


def _canonical_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").casefold()
        parsed_port = parsed.port
    except ValueError:
        return ""
    port = f":{parsed_port}" if parsed_port else ""
    netloc = f"{host}{port}"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, query, ""))


def _same_portal(left: str, right: str) -> bool:
    left_origin = _origin(left)
    return bool(left_origin and left_origin == _origin(right))


def _same_link_destination(link_url: str, snapshot: Multi24Snapshot) -> bool:
    if not _is_multi24_url(snapshot.requested_url):
        return False
    requested = _canonical_url(snapshot.requested_url)
    linked = _canonical_url(link_url)
    if not requested or not linked or requested != linked:
        return False
    final_url = _snapshot_url(snapshot)
    if not _is_multi24_url(final_url) or not _same_portal(link_url, final_url):
        return False
    linked_query = _query_values(link_url)
    final_query = _query_values(final_url)
    for key in ("entidade", "id"):
        if linked_query.get(key, []) != final_query.get(key, []):
            return False
    return True


def _has_link_menu(soup: BeautifulSoup) -> bool:
    for tag in soup.find_all(True):
        classes = tag.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        if any(value.casefold() == "link_menu" for value in classes):
            return True
    return False


def _identity_evidence(page: _ParsedPage, municipio: str) -> tuple[str, ...]:
    expected = _norm(municipio)
    segments: list[str] = []
    for tag in page.soup.find_all(["title", "h1", "h2", "h3"]):
        if isinstance(tag, Tag):
            text = _norm(tag.get_text(" ", strip=True))
            if text:
                segments.append(text)
    for header in page.soup.find_all("header"):
        if not isinstance(header, Tag):
            continue
        for tag in header.find_all(["h1", "h2", "h3", "p", "span", "address"]):
            if isinstance(tag, Tag):
                text = _norm(tag.get_text(" ", strip=True))
                if text:
                    segments.append(text)

    declarations: set[str] = set()
    declaration_pattern = re.compile(r"\b(?:municipio|prefeitura(?:\s+municipal)?)\s+de\s+(.+)$")
    for segment in segments:
        match = declaration_pattern.search(segment)
        if not match:
            continue
        declared = re.split(
            r"\s+(?:[-|;,])\s+|:\s*",
            match.group(1),
            maxsplit=1,
        )[0].strip(" .")
        if declared:
            declarations.add(declared)

    # A matching footer/body mention cannot override a contradictory title or
    # header declaration. Ambiguous authoritative identities fail closed.
    if expected not in declarations or any(value != expected for value in declarations):
        return ()
    return (f"municipio:{expected}",)


def _platform_evidence(page: _ParsedPage) -> tuple[str, ...]:
    evidence: list[str] = []
    if _MULTI24_PATH_RE.search(urlsplit(page.url).path):
        evidence.append("path:multi24_transparencia")
    query = _query_values(page.url)
    if query.get("secao") and query.get("id"):
        evidence.append("query:secao_dinamico_and_id")
    if "portal da transparencia" in page.normalized_text:
        evidence.append("interface:portal_da_transparencia")
    if _has_link_menu(page.soup):
        evidence.append("interface:link_menu")
    if any(marker in page.normalized_text for marker in ("mapa do portal", "trocar municipio", "acesso rapido")):
        evidence.append("interface:portal_menu_marker")
    host = (urlsplit(page.url).hostname or "").casefold()
    if host == "multi24h.com.br" or host.endswith(".multi24h.com.br"):
        evidence.append("host:multi24h")
    return tuple(evidence)


def _page_failure(page: _ParsedPage, municipio: str, prefix: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    identity = _identity_evidence(page, municipio)
    platform = _platform_evidence(page)
    if page.snapshot.status_code != 200:
        return f"{prefix}_status_not_200", identity, platform
    if not _is_multi24_url(page.url):
        return f"{prefix}_url_not_multi24", identity, platform
    if any(term in page.normalized_text for term in _SOFT_404_TERMS):
        return f"{prefix}_soft_404", identity, platform
    required_platform = {
        "path:multi24_transparencia",
        "query:secao_dinamico_and_id",
        "interface:portal_da_transparencia",
    }
    if not required_platform.issubset(platform) or not (
        {"interface:link_menu", "interface:portal_menu_marker"} & set(platform)
    ):
        return f"{prefix}_platform_signature_missing", identity, platform
    if not identity:
        return f"{prefix}_identity_mismatch", identity, platform
    return "", identity, platform


def _direct_anchor(li: Tag) -> Tag | None:
    for child in li.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "a":
            return child
        if child.name in {"span", "div"}:
            nested = child.find("a", recursive=False)
            if isinstance(nested, Tag):
                return nested
        if child.name in {"ul", "ol"}:
            break
    return None


def _label(anchor: Tag) -> str:
    return re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()


def _provenance(anchor: Tag) -> tuple[str, ...]:
    ancestors = [parent for parent in anchor.parents if isinstance(parent, Tag) and parent.name == "li"]
    labels: list[str] = []
    for li in reversed(ancestors):
        direct = _direct_anchor(li)
        if direct is not None:
            value = _label(direct)
            if value and (not labels or value != labels[-1]):
                labels.append(value)
    current = _label(anchor)
    if current and (not labels or current != labels[-1]):
        labels.append(current)
    return tuple(labels)


def _parent_source_url(anchor: Tag, page_url: str) -> str:
    current_li = anchor.find_parent("li")
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name != "li" or parent is current_li:
            continue
        parent_anchor = _direct_anchor(parent)
        if parent_anchor is None or not parent_anchor.has_attr("href"):
            continue
        target = urljoin(page_url, str(parent_anchor["href"]))
        if _is_multi24_url(target) and _same_portal(page_url, target):
            return target
    return page_url


_NAVIGATION_CONTAINER_TOKENS = frozenset(
    {
        "menu",
        "menu_portal",
        "menu-portal",
        "mapa_portal",
        "mapa-portal",
        "portal_menu",
        "portal-menu",
        "breadcrumb",
        "caminho",
        "migalha",
    }
)


def _has_navigation_container_marker(tag: Tag) -> bool:
    classes = tag.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    identifier = str(tag.get("id", "")).casefold()
    aria = _norm(str(tag.get("aria-label", "")))
    normalized_classes = {str(value).casefold() for value in classes}
    return (
        identifier in _NAVIGATION_CONTAINER_TOKENS
        or bool(normalized_classes & _NAVIGATION_CONTAINER_TOKENS)
        or aria in {"mapa do portal", "mapa portal", "menu do portal", "menu portal"}
    )


def _is_navigation_anchor(anchor: Tag) -> bool:
    classes = anchor.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    if any(str(value).casefold() == "link_menu" for value in classes):
        return True
    return any(
        _has_navigation_container_marker(parent)
        for parent in anchor.parents
        if isinstance(parent, Tag)
    )


def _extract_edges(page: _ParsedPage) -> tuple[Multi24Edge, ...]:
    edges: list[Multi24Edge] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for anchor in page.soup.find_all("a", href=True):
        if not _is_navigation_anchor(anchor):
            continue
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        # BeautifulSoup has already decoded HTML entities once.  Calling
        # html.unescape here would corrupt a literal ampersand entity twice.
        target = urljoin(page.url, href)
        if not _is_multi24_url(target) or not _same_portal(page.url, target):
            continue
        labels = _provenance(anchor)
        if not labels:
            continue
        key = (_canonical_url(target), labels)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            Multi24Edge(
                source_url=_parent_source_url(anchor, page.url),
                target_url=target,
                label=labels[-1],
                provenance=labels,
                depth=max(0, len(labels) - 1),
            )
        )
    return tuple(edges)


def _year_in_text(value: str, year: int) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(str(year))}(?!\d)", value))


def _has_excluded_term(normalized: str) -> bool:
    return any(pattern.search(normalized) for pattern in _EXCLUDED_BRANCH_PATTERNS)


def _matched_item_kinds(normalized: str) -> tuple[str, ...]:
    return tuple(kind for kind, pattern in _ITEM_TERM_PATTERNS if pattern.search(normalized))


def _classify_path(labels: tuple[str, ...]) -> str:
    if any(_has_excluded_term(_norm(label)) for label in labels):
        return "excluded"
    ambiguous_seen = False
    for label in reversed(labels):
        value = _norm(label)
        has_cp = bool(re.search(r"\bconcursos?\s+publicos?\b", value))
        has_ps = bool(
            re.search(r"\bprocessos?\s+seletivos?(?:\s+publicos?)?\b", value)
            or re.search(r"\b(?:pss|psp)\b", value)
        )
        if has_cp and has_ps:
            ambiguous_seen = True
            continue
        if has_ps:
            return "processo_seletivo"
        if has_cp:
            return "concurso_publico"
    return "ambiguous" if ambiguous_seen else ""


def _breadcrumb_texts(soup: BeautifulSoup) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all(True):
        classes = tag.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        marker = " ".join(
            [str(tag.get("id", "")), str(tag.get("aria-label", "")), *[str(value) for value in classes]]
        )
        marker = _norm(marker)
        if not any(token in marker for token in ("breadcrumb", "caminho", "migalha")):
            continue
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        normalized = _norm(text)
        if text and normalized not in seen:
            seen.add(normalized)
            values.append(text)
    return tuple(values)


def _breadcrumb_matches(page: _ParsedPage, edge: Multi24Edge, bucket: str, year: int) -> bool:
    breadcrumbs = _breadcrumb_texts(page.soup)
    for breadcrumb in breadcrumbs:
        normalized = _norm(breadcrumb)
        if not _year_in_text(normalized, year):
            continue
        leaf = _norm(edge.label)
        if leaf != str(year) and not re.search(
            rf"(?<![\w-]){re.escape(leaf)}(?![\w-])", normalized
        ):
            continue
        if bucket == "concurso_publico" and re.search(r"\bconcursos?\s+publicos?\b", normalized):
            return True
        if bucket == "processo_seletivo" and (
            re.search(r"\bprocessos?\s+seletivos?\b", normalized)
            or re.search(r"\b(?:pss|psp)\b", normalized)
        ):
            return True
    return False


def _content_roots(soup: BeautifulSoup) -> tuple[Tag, ...]:
    for selector in (
        "main",
        "[role='main']",
        "#conteudo",
        ".conteudo",
        "#content",
        ".content",
        "article",
    ):
        matches = tuple(tag for tag in soup.select(selector) if isinstance(tag, Tag))
        if matches:
            return matches
    return ()


def _is_navigation_element(tag: Tag, root: Tag) -> bool:
    for parent in (tag, *[ancestor for ancestor in tag.parents if isinstance(ancestor, Tag)]):
        if parent.name in {"nav", "aside"} or _has_navigation_container_marker(parent):
            return True
        if parent is root:
            return False
    return False


def _item_positive(title: str, year: int, bucket: str) -> bool:
    normalized = _norm(title)
    if not _year_in_text(normalized, year):
        return False
    classified = _classify_path((title,))
    if classified in {"excluded", "ambiguous"} or (
        classified in VALID_BUCKETS and classified != bucket
    ):
        return False
    if not _matched_item_kinds(normalized):
        return False
    numbered = re.search(rf"\b\d{{1,4}}\s*[/.-]\s*{year}\b", normalized)
    dated = re.search(rf"\b\d{{1,2}}\s*/\s*\d{{1,2}}\s*/\s*{year}\b", normalized)
    return bool(numbered or dated)


def _item_evidence_key(title: str, year: int) -> str:
    normalized = _norm(title)
    kinds = tuple(sorted(_matched_item_kinds(normalized)))
    numbered = re.search(rf"\b\d{{1,4}}\s*[/.-]\s*{year}\b", normalized)
    dated = re.search(rf"\b\d{{1,2}}\s*/\s*\d{{1,2}}\s*/\s*{year}\b", normalized)
    identifier = (dated or numbered).group(0) if (dated or numbered) else ""
    return f"{'|'.join(kinds)}:{identifier}"


def _extract_items(page: _ParsedPage, year: int, bucket: str) -> tuple[Multi24Item, ...]:
    items: list[Multi24Item] = []
    seen: set[str] = set()
    for root in _content_roots(page.soup):
        for tag in root.find_all(["a", "tr", "li"]):
            if not isinstance(tag, Tag) or _is_navigation_element(tag, root):
                continue
            if tag.name in {"tr", "li"} and tag.find("a", href=True) is not None:
                # The descendant anchor is the atomic evidence. Counting its
                # row/list container as well could turn one document into two.
                continue
            title = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
            normalized = _norm(title)
            if not title or not _item_positive(title, year, bucket):
                continue
            url = ""
            if tag.name == "a" and tag.has_attr("href"):
                candidate_url = urljoin(page.url, str(tag["href"]))
                if urlsplit(candidate_url).scheme.casefold() in {"http", "https"}:
                    url = candidate_url
            # Semantic identity is safer than URL identity: the same document
            # is often linked twice with different cache/query parameters.
            evidence_key = _item_evidence_key(title, year)
            if not evidence_key or evidence_key in seen:
                continue
            seen.add(evidence_key)
            items.append(Multi24Item(title=title, url=url))
    return tuple(items)


def _linked_snapshot_index(linked_pages: Mapping[str, Multi24Snapshot]) -> dict[str, Multi24Snapshot]:
    index: dict[str, Multi24Snapshot] = {}
    for key, snapshot in linked_pages.items():
        if not isinstance(key, str) or not isinstance(snapshot, Multi24Snapshot):
            raise Multi24ContractError("linked_pages_must_map_urls_to_snapshots")
        for url in (key, snapshot.requested_url):
            if url.strip():
                index.setdefault(_canonical_url(url), snapshot)
    return index


def _metadata_rows(pages: list[_ParsedPage]) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    hashes: list[tuple[str, str]] = []
    charsets: list[tuple[str, str]] = []
    header_mismatch: list[str] = []
    meta_mismatch: list[str] = []
    repaired: list[str] = []
    seen: set[str] = set()
    for page in pages:
        if page.url in seen:
            continue
        seen.add(page.url)
        hashes.append((page.url, page.decoded.raw_sha256))
        charsets.append((page.url, page.decoded.charset_used))
        if page.decoded.header_mismatch:
            header_mismatch.append(page.url)
        if page.decoded.meta_mismatch:
            meta_mismatch.append(page.url)
        if page.decoded.mojibake_repaired:
            repaired.append(page.url)
    return tuple(hashes), tuple(charsets), tuple(header_mismatch), tuple(meta_mismatch), tuple(repaired)


def _authority_check(
    page: _ParsedPage,
    authority: Multi24Authority,
    municipio: str,
) -> tuple[str, tuple[str, ...]]:
    entry_origin = _origin(page.url)
    if not entry_origin or _origin(page.snapshot.requested_url) != entry_origin:
        return "entry_origin_not_authorized", ()

    official_origins = {_origin(value) for value in authority.official_source_origins}
    official_origins.discard("")
    for proof in authority.navigation_snapshots:
        proof_page = _parse_snapshot(proof)
        if (
            proof.status_code != 200
            or _origin(proof.requested_url) not in official_origins
            or _origin(proof_page.url) not in official_origins
        ):
            continue
        if any(term in proof_page.normalized_text for term in _SOFT_404_TERMS):
            continue
        if not _identity_evidence(proof_page, municipio):
            continue
        for anchor in proof_page.soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            target = urljoin(proof_page.url, href)
            if _origin(target) != entry_origin:
                continue
            link_text = _norm(anchor.get_text(" ", strip=True))
            target_path = urlsplit(target).path.casefold()
            exact_target = _canonical_url(target) in {
                _canonical_url(page.snapshot.requested_url),
                _canonical_url(page.url),
            }
            semantic_target = (
                _MULTI24_PATH_RE.search(target_path) is not None
                or "/portal-da-transparencia" in target_path
                or any(
                    marker in link_text
                    for marker in ("portal da transparencia", "concurso", "processo seletivo")
                )
            )
            if not exact_target and not semantic_target:
                continue
            anchor_label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
            evidence = (
                f"official_source:{proof_page.url}",
                f"official_source_sha256:{proof_page.decoded.raw_sha256}",
                f"official_navigation_target:{target}",
                f"official_navigation_label:{anchor_label}",
                f"navigation_target_origin:{entry_origin}",
            )
            return "", evidence
    return "official_navigation_link_not_proven", ()


def _validate_contract(
    entry: Multi24Snapshot,
    linked_pages: Mapping[str, Multi24Snapshot],
    authority: Multi24Authority,
    municipio: str,
    bucket: str,
    current_year: int,
) -> None:
    if not isinstance(entry, Multi24Snapshot):
        raise Multi24ContractError("entry_must_be_multi24_snapshot")
    if not isinstance(entry.body, bytes):
        raise Multi24ContractError("snapshot_body_must_be_bytes")
    if not isinstance(linked_pages, Mapping):
        raise Multi24ContractError("linked_pages_must_be_mapping")
    if not isinstance(authority, Multi24Authority):
        raise Multi24ContractError("authority_must_be_multi24_authority")
    if not authority.official_source_origins:
        raise Multi24ContractError("official_source_origins_required")
    if any(not _origin(value) for value in authority.official_source_origins):
        raise Multi24ContractError("official_source_origin_invalid")
    if not authority.navigation_snapshots:
        raise Multi24ContractError("official_navigation_evidence_required")
    for proof in authority.navigation_snapshots:
        if not isinstance(proof, Multi24Snapshot) or not isinstance(proof.body, bytes):
            raise Multi24ContractError("navigation_snapshots_must_be_byte_snapshots")
        if not proof.requested_url.strip():
            raise Multi24ContractError("navigation_snapshot_requested_url_required")
    if not isinstance(municipio, str) or not municipio.strip():
        raise Multi24ContractError("municipio_must_be_non_empty")
    if bucket not in VALID_BUCKETS:
        raise Multi24ContractError("invalid_bucket")
    if isinstance(current_year, bool) or not isinstance(current_year, int) or not 1900 <= current_year <= 2200:
        raise Multi24ContractError("invalid_current_year")
    if not entry.requested_url.strip():
        raise Multi24ContractError("entry_requested_url_required")


def analyze_multi24(
    *,
    entry: Multi24Snapshot,
    linked_pages: Mapping[str, Multi24Snapshot],
    authority: Multi24Authority,
    municipio: str,
    bucket: str,
    current_year: int,
) -> Multi24Result:
    """Analyse a supplied Multi24 navigation tree without performing I/O.

    Only child snapshots reached through a real HTML edge are considered.  A
    caller may supply extra pages, but unlinked/orphan snapshots are ignored.
    """

    _validate_contract(entry, linked_pages, authority, municipio, bucket, current_year)
    linked_index = _linked_snapshot_index(linked_pages)
    entry_page = _parse_snapshot(entry)
    processed_pages = [entry_page]
    entry_failure, identity, platform = _page_failure(entry_page, municipio, "entry")
    authority_failure, authority_evidence = _authority_check(entry_page, authority, municipio)
    if authority_failure:
        entry_failure = authority_failure

    if entry_failure:
        hashes, charsets, header_mismatch, meta_mismatch, repaired = _metadata_rows(processed_pages)
        return Multi24Result(
            disposition="revisar",
            reason=entry_failure,
            review_reasons=(entry_failure,),
            municipio=municipio,
            bucket=bucket,
            current_year=current_year,
            index_url="",
            edges=(),
            candidates=(),
            authority_evidence=authority_evidence,
            identity_evidence=identity,
            platform_evidence=platform,
            raw_sha256_by_url=hashes,
            charset_by_url=charsets,
            header_mismatch_urls=header_mismatch,
            meta_mismatch_urls=meta_mismatch,
            mojibake_repaired_urls=repaired,
        )

    edges = _extract_edges(entry_page)
    current_edges = tuple(
        edge
        for edge in edges
        if _year_in_text(_norm(edge.label), current_year) and _classify_path(edge.provenance) == bucket
    )
    review_reasons: list[str] = []
    candidates: list[Multi24Candidate] = []

    if not current_edges:
        review_reasons.append("no_linked_current_year_node")

    for edge in current_edges:
        child = linked_index.get(_canonical_url(edge.target_url))
        if child is None:
            review_reasons.append("linked_current_year_page_missing")
            continue
        if not _same_link_destination(edge.target_url, child):
            review_reasons.append("linked_page_destination_mismatch")
            continue

        child_page = _parse_snapshot(child)
        processed_pages.append(child_page)
        child_failure, _, _ = _page_failure(child_page, municipio, "linked_page")
        if child_failure:
            review_reasons.append(child_failure)
            continue
        if not _breadcrumb_matches(child_page, edge, bucket, current_year):
            review_reasons.append("linked_page_breadcrumb_mismatch")
            continue

        items = _extract_items(child_page, current_year, bucket)
        if len(items) < MIN_ITEM_EVIDENCE:
            review_reasons.append("linked_page_insufficient_item_evidence")
            continue
        candidates.append(
            Multi24Candidate(
                node_url=child_page.url,
                label=edge.label,
                bucket=bucket,
                provenance=edge.provenance,
                items=items,
            )
        )

    unique_reasons = tuple(dict.fromkeys(review_reasons))
    hashes, charsets, header_mismatch, meta_mismatch, repaired = _metadata_rows(processed_pages)
    disposition = "candidata" if candidates else "revisar"
    reason = "candidate_nodes_with_item_evidence" if candidates else (
        unique_reasons[0] if unique_reasons else "no_candidate_with_item_evidence"
    )
    return Multi24Result(
        disposition=disposition,
        reason=reason,
        review_reasons=unique_reasons,
        municipio=municipio,
        bucket=bucket,
        current_year=current_year,
        index_url=entry_page.url if candidates else "",
        edges=edges,
        candidates=tuple(candidates),
        authority_evidence=authority_evidence,
        identity_evidence=identity,
        platform_evidence=platform,
        raw_sha256_by_url=hashes,
        charset_by_url=charsets,
        header_mismatch_urls=header_mismatch,
        meta_mismatch_urls=meta_mismatch,
        mojibake_repaired_urls=repaired,
    )


__all__ = [
    "MIN_ITEM_EVIDENCE",
    "Multi24Authority",
    "Multi24Candidate",
    "Multi24ContractError",
    "Multi24DecodeInfo",
    "Multi24Edge",
    "Multi24Item",
    "Multi24Result",
    "Multi24Snapshot",
    "VALID_BUCKETS",
    "VALID_DISPOSITIONS",
    "analyze_multi24",
    "decode_snapshot",
]
