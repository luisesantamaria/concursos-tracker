"""Autoridad general basada en evidencia HTTP objetiva (redirect provenance).

Directiva 12-jul (independencia V1): este modulo NO adjudica contenido ni
semantica. Emite unicamente ``official_referrer`` provenance -- un hecho
estructural (un 30x real capturado por :class:`OrionHTTPFetcher`, cuyo host de
ORIGEN es el dominio oficial *.rs.gov.br confirmado del municipio) que
``cascade._provenance_confirms``/``cascade._candidate_source_and_authority``
ya sabe leer (kind ``official_referrer`` esta en la lista aceptada desde antes;
nadie lo emitia). Caso real: Porto Alegre redirige 301/302 x4 desde
smap/servicos/... en ``portoalegre.rs.gov.br`` hasta ``prefeitura.poa.br``
(200); el fetcher ya captura ``requested_url``/``final_url`` pero
``_candidate()`` no pasaba provenance, asi que el host no-rs.gov.br quedaba
con authority='desconocida' y el gate estructural rechazaba pese a A/B en
consenso correcto.

No se importa ``verdict_extract`` ni ningun modulo semantico V1; solo se
reutilizan las funciones NEUTRALES de ``cascade_municipios`` ya permitidas
(``clean_url``, ``Page``, ``is_matching_official_municipality_domain``), en
el mismo patron que ``cascade._official_host``.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from urllib.parse import urlparse

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.shared import scope_rs


_REGISTRY_PATH = Path(__file__).resolve().parent / "data" / "dominios_oficiales_rs.csv"
_REGISTRY_COLUMNS = ("municipio", "host", "evidencia", "verificado_por", "fecha")
_registry_cache: list[dict] | None = None


def _lower_host(url: str) -> str:
    """Hostname en minuscula sin punto final; cadena vacia si no hay host."""
    return (urlparse(cascade.clean_url(url or "")).hostname or "").lower().rstrip(".")


def redirect_confirms_official_origin(
    requested_url: str, final_url: str, municipio: str
) -> bool:
    """True solo cuando un 30x real parte de un dominio oficial confirmado.

    Evidencia estructural, no opinion: requiere (1) ambos hosts presentes,
    (2) un cambio real de host (un redirect que se queda en el mismo host no
    es una cadena de delegacion), y (3) que el host de ORIGEN (no el
    destino) sea el dominio oficial *.rs.gov.br del municipio segun el mismo
    chequeo de identidad que usa Tier 0 (``is_matching_official_municipality_
    domain``, via una ``Page`` sonda -- mismo patron que ``cascade._official_
    host``). El host de destino puede ser cualquier cosa (portal delegado,
    dominio .com.br propio, etc.): eso es exactamente lo que esta provenance
    existe para confirmar.
    """
    if not requested_url or not final_url or not municipio:
        return False
    requested_host = _lower_host(requested_url)
    final_host = _lower_host(final_url)
    if not requested_host or not final_host:
        return False
    if requested_host == final_host:
        return False
    # Exclusion trivial de destinos evidentemente no relacionados (redes
    # sociales, buscadores, agregadores) reutilizando la lista ya existente
    # de cascade -- no se inventa una lista nueva.
    if any(bad in final_host for bad in cascade.BAD_HOSTS):
        return False
    probe = cascade.Page(url=cascade.clean_url(requested_url), status=200)
    return cascade.is_matching_official_municipality_domain(probe, municipio)


def redirect_provenance(
    requested_url: str, final_url: str, municipio: str
) -> tuple[dict, ...]:
    """Provenance ``official_referrer`` (aceptada por ``_provenance_confirms``)
    o tupla vacia cuando el redirect no confirma origen oficial."""
    if not redirect_confirms_official_origin(requested_url, final_url, municipio):
        return ()
    origin_host = _lower_host(requested_url)
    dest_host = _lower_host(final_url)
    return (
        {
            "kind": "official_referrer",
            "municipio": municipio,
            "referrer": requested_url,
            "label": "http_redirect_official_origin",
            "evidence": f"{origin_host} -> {dest_host}",
        },
    )


# ---------------------------------------------------------------------------
# Registro versionado de dominios oficiales (provenance por REGISTRO)
# ---------------------------------------------------------------------------
# Directiva Luis/Orion (12-jul): registro versionado de dominios oficiales +
# cadena verificable + provenance -- quien, cuando, con que evidencia. El CSV
# (``data/dominios_oficiales_rs.csv``) asienta HECHOS curados a mano -- que
# host es el dominio oficial de que municipio -- nunca la decision
# confirmar/revisar, que se sigue ganando por adjudicacion A/B/C en vivo. Este
# modulo solo consulta el registro; no clasifica contenido.
#
# Distincion de seguridad frente a ``redirect_provenance`` (arriba): la
# provenance de REDIRECT prueba cadena de autoridad pero NUNCA identidad -- un
# open-redirect desde el dominio oficial hacia contenido de OTRO municipio no
# debe lavar identity (revision adversarial Opus 12-jul, ver
# ``test_redirect_provenance_never_launders_identity_cross_municipio``). La
# provenance de REGISTRO, en cambio, SI puede probar identidad: el binding
# host<->municipio esta curado por un humano (columna ``verificado_por``) y
# nadie que solo controle una pagina remota puede anadir una fila al CSV. Por
# eso ``structural_evidence.structural_candidate`` puede tratar un match de
# registro como identidad confirmada incluso cuando la pagina misma no
# menciona el municipio (shells SPA client-rendered), mientras que un match de
# redirect nunca alcanza para eso.


def _normalize_host(host: str) -> str:
    """Host en minuscula, sin punto final ni prefijo ``www.``."""
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("www."):
        h = h[len("www."):]
    return h


def _load_registry() -> list[dict]:
    """Carga perezosa (module-level cache) del registro de dominios oficiales."""
    global _registry_cache
    if _registry_cache is None:
        rows: list[dict] = []
        if _REGISTRY_PATH.exists():
            with _REGISTRY_PATH.open(encoding="utf-8-sig", newline="") as fh:
                rows = [dict(row) for row in csv.DictReader(fh)]
        _registry_cache = rows
    return _registry_cache


def registry_official_host(municipio: str, host: str) -> dict | None:
    """Fila del registro cuyo host coincide con ``host`` para ``municipio``.

    Coincide si el host normalizado es igual al ``entry.host`` del registro o
    termina en ``"." + entry.host`` (sufijo de subdominio). El municipio se
    compara con ``cascade.norm()`` (case/acento/conector-insensitive). Devuelve
    la fila completa (para poder citar su evidencia) o ``None`` si no hay
    match -- nunca inventa una fila.
    """
    target_municipio = cascade.norm(municipio or "")
    target_host = _normalize_host(host)
    if not target_municipio or not target_host:
        return None
    for row in _load_registry():
        if cascade.norm(row.get("municipio", "")) != target_municipio:
            continue
        entry_host = _normalize_host(row.get("host", ""))
        if not entry_host:
            continue
        if target_host == entry_host or target_host.endswith("." + entry_host):
            return row
    return None


def registry_provenance(municipio: str, final_url: str) -> tuple[dict, ...]:
    """Provenance ``official_brand`` (aceptada por ``_provenance_confirms``)
    cuando el host de ``final_url`` matchea el registro para ``municipio``, o
    tupla vacia si no hay match."""
    host = _normalize_host(_lower_host(final_url))
    entry = registry_official_host(municipio, host)
    if not entry:
        return ()
    return (
        {
            "kind": "official_brand",
            "municipio": municipio,
            "label": "registry:dominios_oficiales_rs.csv",
            "evidence": f"{host} verificado: {entry.get('evidencia', '')}",
        },
    )


# ---------------------------------------------------------------------------
# Mision D (12-jul) -- cobertura de autoridad en municipios NUEVOS (fuera del
# golden set, por lo tanto fuera de ``dominios_oficiales_rs.csv``). Replay
# offline de holdout50_20260712 (``eval/replay_final_gate.py``) confirmo que
# 39/41 unidades donde A certifico con citas verificadas y B sostuvo, pero el
# gate rechazo, caian por authority='desconocida' -- nunca por un defecto de
# C pisando el consenso (esa hipotesis quedo descartada por el mismo replay).
# Dos fuentes de verdad NUEVAS, generales y con provenance, nunca hardcodes
# por municipio:
#   (a) plataforma delegada (atende.net/multi24h.com.br): SOLO autoridad.
#   (b) universo (data/fase2/municipios_rs_local.csv, columna site_base):
#       autoridad + un camino adicional de identidad, pero solo cuando el
#       contenido de la pagina TAMBIEN confirma -- nunca solo por el host.
# ---------------------------------------------------------------------------

_UNIVERSE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fase2" / "municipios_rs_local.csv"
)
_universe_cache: dict[str, str] | None = None


def _load_universe_site_base() -> dict[str, str]:
    """Lazy-loaded ``norm(municipio) -> host(site_base)`` from the Tier-0
    discovery universe. ``site_base`` is the fase 2 pipeline's OWN Tier-0/
    Tier-2 guess at the prefeitura's base domain -- ``municipios_rs_local.csv``
    is pipeline OUTPUT (method/confianza/razao/checked_at columns), not a
    hand-curated registry like ``dominios_oficiales_rs.csv``. A row is only
    trusted here when the SAME row shows the pipeline actually confirmed at
    least one bucket for this municipality (``confianza_concursos`` or
    ``confianza_processos`` == ``"confirmado"``); otherwise ``site_base`` can
    be a generic third-party directory or a dead stub the pipeline itself
    gave up on -- real rows in this exact CSV: Muliterno ->
    ``cidade-brasil.com.br`` (a national city directory, not a prefeitura)
    and Vista Gaucha -> a web-agency domain, both with empty confianza and
    notes ``"no valid index page found"``. Without this gate those rows would
    silently become an authority+identity source for whatever this candidate
    page happens to render."""
    global _universe_cache
    if _universe_cache is None:
        table: dict[str, str] = {}
        if _UNIVERSE_PATH.exists():
            with _UNIVERSE_PATH.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    municipio = row.get("municipio", "")
                    host = _normalize_host(_lower_host(row.get("site_base", "")))
                    confirmed = (
                        row.get("confianza_concursos", "") == "confirmado"
                        or row.get("confianza_processos", "") == "confirmado"
                    )
                    if municipio and host and confirmed:
                        table[cascade.norm(municipio)] = host
        _universe_cache = table
    return _universe_cache


def universe_site_base_host(municipio: str) -> str:
    """Host recorded as ``site_base`` for ``municipio`` in the discovery
    universe, or "" when the municipality has no recorded site_base."""
    return _load_universe_site_base().get(cascade.norm(municipio or ""), "")


def universe_site_base_match(municipio: str, final_url: str) -> bool:
    """True when the candidate's host equals this municipality's own
    site_base host (never a different municipality's -- the lookup is keyed
    by ``municipio`` itself, so no cross-municipio leakage is possible)."""
    host = _normalize_host(_lower_host(final_url))
    base_host = universe_site_base_host(municipio)
    return bool(host and base_host and host == base_host)


def universe_provenance(municipio: str, final_url: str) -> tuple[dict, ...]:
    """Provenance ``official_brand`` (kind aceptado por
    ``cascade._provenance_confirms``) cuando el host candidato es el
    ``site_base`` registrado del universo para ESTE municipio. Regla (b) de
    Mision D: confirma AUTORIDAD (via ``_candidate_source_and_authority``,
    para hosts no .rs.gov.br) -- nunca identidad por si sola; ver
    ``universe_identity_confirms`` para el camino de identidad, que exige
    ademas el chequeo de contenido."""
    if not universe_site_base_match(municipio, final_url):
        return ()
    host = _normalize_host(_lower_host(final_url))
    return (
        {
            "kind": "official_brand",
            "municipio": municipio,
            "label": "universo_site_base",
            "evidence": f"{host} == site_base (data/fase2/municipios_rs_local.csv)",
        },
    )


def universe_identity_confirms(municipio: str, final_url: str, page: "cascade.Page") -> bool:
    """Escape hatch for ``cascade._candidate_identity_state``'s .rs.gov.br
    slug-label check, which returns 'rechazada' the instant
    ``cascade.slugify(municipio)`` is not one of the host's dot-separated
    labels -- BEFORE ever reading the page's own content. ``slugify`` strips
    connector words (da/de/do/das/dos), but several real official hosts keep
    them (``arroiodosal.rs.gov.br`` for "Arroio do Sal",
    ``saodomingosdosul.rs.gov.br`` for "Sao Domingos do Sul"), or use an
    unrelated abbreviation (``pmfv.rs.gov.br`` for "Fortaleza dos Valos"), so
    a genuine official page for the requested municipality was rejected
    sight unseen.

    Two independent facts must BOTH hold; neither alone is enough:
    (1) ``universe_site_base_match`` -- the candidate host equals this
        municipio's own site_base (a prior structural fact, RS-scoped, keyed
        by this exact municipio -- a homonym in another UF simply is not in
        this table under this key);
    (2) the municipality's own name literally appears in this page's
        title/body -- the SAME content bar cascade already applies to
        non-.rs.gov.br hosts, just not gated behind the slug-label match.
    Fails closed like everything else here: without BOTH, no identity.
    """
    if not universe_site_base_match(municipio, final_url):
        return False
    target = cascade.norm(municipio or "")
    blob = cascade.norm(f"{page.title}\n{page.text[:3000]}")
    return bool(target and target in blob)


# ---------------------------------------------------------------------------
# Regla (a): plataforma delegada (atende.net / multi24h.com.br). Precedente
# golden: Aceguá ("atende.net = portal delegado oficial").
# ---------------------------------------------------------------------------

_DELEGATED_PLATFORM_SUFFIXES = (".atende.net", ".multi24h.com.br")


def _municipio_concat_slug(municipio: str) -> str:
    """Full-concatenation slug (accents/case stripped, connector words
    KEPT) -- the convention observed on real atende.net/multi24h.com.br
    municipal subdomains (``saopedrodosul``, ``cristaldosul``,
    ``arroiodosal``, ``doisirmaos``). Deliberately DIFFERENT from
    ``cascade.slugify``, which drops da/de/do/das/dos and is tuned for
    *.rs.gov.br hosts instead -- reusing it here would silently break every
    connector-word municipality on these platforms."""
    return re.sub(r"[^a-z0-9]", "", cascade.norm(municipio or ""))


def _content_signals_other_uf(page: "cascade.Page") -> bool:
    """True when the page's OWN rendered content names a Brazilian UF/state
    other than RS, without also naming RS.

    This is the general tell that a delegated-platform host actually serves
    a homonym municipality in a DIFFERENT state: atende.net/multi24h.com.br
    host municipalities from every UF by subdomain slug, and EXACT-name
    homonyms across UFs are common (Bom Jesus, Santa Maria, Vera Cruz, Boa
    Vista...). For a genuine homonym the content-based identity check is
    blind -- the target municipio's name genuinely appears on the page,
    because that other city really is called that too. The only remaining
    structural signal is the state itself: real prefeitura pages almost
    always declare their own UF/state name somewhere in title or body.
    Reuses the exact UF/state-name detection ``scripts.shared.scope_rs``
    already applies to scope national banca candidates to RS -- one
    definition of "this smells like another state", not a fase2-only
    reinvention.
    """
    blob = f"{page.title}\n{page.text[:3000]}"
    if re.search(
        r"(?:/|\b-|[-\s])\s*RS\b|rio grande do sul|\.rs\.gov\.br", blob, re.I,
    ):
        return False
    other_uf_pattern = (
        r"(?:/|\b-|[-\s])\s*(" + "|".join(sorted(scope_rs.OTHER_UFS)) + r")\b"
    )
    if re.search(other_uf_pattern, blob, re.I):
        return True
    normalized = scope_rs.normalize_text(blob)
    return any(
        re.search(rf"\b{re.escape(state)}\b", normalized)
        for state in scope_rs.OTHER_STATE_NAMES
    )


def delegated_platform_provenance(
    municipio: str, final_url: str, page: "cascade.Page | None" = None,
) -> tuple[dict, ...]:
    """Provenance ``official_brand`` confirming AUTHORITY ONLY when a
    delegated-platform host's subdomain slug equals this municipality's own
    full-concatenation slug.

    SEGURIDAD ANTI-FP (critico, Mision D; endurecido tras revision
    adversarial): atende.net/multi24h.com.br sirven municipios de TODO
    Brasil, y existen homonimos EXACTOS entre UFs (p.ej. "Bom Jesus" existe
    en RS, PI y SC). El slug NUNCA prueba identidad por si mismo -- solo que
    la plataforma es un canal delegado legitimo (mismo principio que
    ``redirect_provenance``: cadena de autoridad, no identidad del destino).
    La identidad sigue exigiendo, como siempre, que el contenido de la
    propia pagina mencione el municipio: este modulo NUNCA alimenta esta
    provenance a la identidad -- ``structural_evidence.py`` sigue llamando
    ``cascade._candidate_identity_state`` con ``provenance=()`` vacio por
    diseno (directiva 12-jul), asi que ninguna provenance de esta funcion
    puede aparecer ahi sin importar su ``kind``.

    Pero para un homonimo EXACTO (mismo nombre municipal, otra UF) ese
    chequeo de identidad por contenido es ciego: el nombre objetivo
    realmente aparece en la pagina, porque la otra ciudad se llama igual.
    Por eso, cuando se provee ``page`` (el contenido ya fetcheado de
    ``final_url``), esta funcion TAMBIEN niega la propia AUTORIDAD si ese
    contenido declara explicitamente otra UF/estado sin mencionar RS
    (``_content_signals_other_uf``) -- ni siquiera el slug alcanza entonces.
    Sin ``page`` (compatibilidad con llamadas que solo verifican el slug en
    aislamiento) el chequeo de contenido se omite, no se asume nada por
    ausencia de evidencia.
    """
    host = _normalize_host(_lower_host(final_url))
    if not host:
        return ()
    slug = _municipio_concat_slug(municipio)
    if not slug:
        return ()
    for suffix in _DELEGATED_PLATFORM_SUFFIXES:
        if host == f"{slug}{suffix}":
            if page is not None and _content_signals_other_uf(page):
                return ()
            return (
                {
                    "kind": "official_brand",
                    "municipio": municipio,
                    "label": "plataforma_delegada",
                    "evidence": f"{host} slug=={slug} (plataforma delegada oficial)",
                },
            )
    return ()


__all__ = [
    "redirect_confirms_official_origin",
    "redirect_provenance",
    "registry_official_host",
    "registry_provenance",
    "universe_site_base_host",
    "universe_site_base_match",
    "universe_provenance",
    "universe_identity_confirms",
    "delegated_platform_provenance",
    "_content_signals_other_uf",
]
