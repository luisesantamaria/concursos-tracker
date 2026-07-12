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
from pathlib import Path
from urllib.parse import urlparse

from scripts.fase2_municipios import cascade_municipios as cascade


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


__all__ = [
    "redirect_confirms_official_origin",
    "redirect_provenance",
    "registry_official_host",
    "registry_provenance",
]
