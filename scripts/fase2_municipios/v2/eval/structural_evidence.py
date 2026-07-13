"""Evidencia ESTRUCTURAL para el gate V2 — independencia total de V1.

Directiva 12-jul: la autoridad SEMANTICA pertenece exclusivamente a los agentes
A/B/C; el codigo solo verifica hechos objetivos. Este modulo reutiliza las
funciones NEUTRALES de cascade (permitidas explicitamente: autoridad e
identidad basadas en evidencia, deteccion objetiva de challenge/soft-404,
parsing de pagina, hashes) y JAMAS invoca el clasificador semantico de V1
(``verdict_extract`` via ``evaluate_candidate_contract``/``derive_decision``).

Los campos semanticos del ``CandidateRecord`` (``decision``/``page_role``)
quedan como marcadores ``no_adjudicado_v1`` que el gate V2 nunca lee: es el
contenedor tecnico compartido, no una decision.
"""

from __future__ import annotations

from urllib.parse import urlparse

from scripts.fase2_municipios import cascade_municipios as cascade
from scripts.fase2_municipios.v2 import authority


NO_V1_ADJUDICATION = "no_adjudicado_v1"


def structural_candidate(
    *,
    requested_url: str,
    source: str,
    tier: str,
    municipio: str,
    bucket: str,
    evidence: "cascade.EvidenceSnapshot",
    provenance: list[dict] | tuple[dict, ...] = (),
) -> "cascade.CandidateRecord":
    """Espejo exacto del prefijo ESTRUCTURAL de ``build_candidate_record``:

    - estado de evidencia por hechos (HTTP status >= 400, challenge antibot);
    - identidad municipal por evidencia (soft-404/dead-site, host oficial,
      nombre en titulo/texto, provenance);
    - autoridad/procedencia por cadena oficial verificable (nunca slug);

    sin el sufijo semantico (contratos verdict de contenido).
    """
    requested = evidence.requested_url or requested_url
    final_url = cascade.clean_url(evidence.final_url or requested)
    page = cascade._page_from_html(
        final_url, evidence.status, "text/html; charset=UTF-8",
        evidence.html, requested_url=requested,
    )
    page.title = evidence.title or page.title
    page.text = evidence.text or page.text
    page.links = list(evidence.links) or page.links

    evidence_state = (
        "error_fetch"
        if evidence.status is not None and evidence.status >= 400
        # page.is_antibot (calculado en cascade._page_from_html) exige hard
        # markers de challenge real (challenge-platform/cdn-cgi/challenge/
        # _cf_chl_opt/cf_chl_/ddos-guard) o un titulo tipo "just a moment" con
        # body corto (<1500 chars). cascade.is_antibot_challenge() es laxo
        # (basta la palabra "cloudflare" en cualquier parte del blob) y da
        # falso positivo con paginas COMPLETAS que solo cargan un banner de
        # cookies desde cdnjs.cloudflare.com (caso Passo Fundo: 200 OK,
        # contenido integro, bloqueado hoy por esta mencion benigna).
        else "incompleta_antibot" if page.is_antibot
        else evidence.evidence_state
    )
    accessible = evidence_state != "error_fetch"
    # La provenance de REDIRECT (p.ej. official_referrer) prueba la cadena de
    # AUTORIDAD, nunca la identidad del destino: un open-redirect desde el
    # dominio oficial hacia contenido de OTRO municipio no debe lavar identity
    # (revision adversarial Opus 12-jul). La identidad por defecto la demuestra
    # la evidencia del propio destino: host oficial del municipio o su nombre
    # en titulo/contenido.
    #
    # El registro versionado (``authority.registry_official_host``) es la
    # unica excepcion deliberada: es un HECHO curado a mano (host<->municipio),
    # no una opinion de contenido, asi que puede confirmar identidad incluso
    # cuando la pagina misma no menciona el municipio (slug no estandar como
    # pmaratiba/pmpf/caxias, o un shell SPA client-rendered sin nombre
    # renderizado). Un soft-404/dead-site SIEMPRE rechaza primero -- el
    # registro no salva una pagina que objetivamente no responde.
    host_final = (urlparse(final_url).hostname or "").lower().rstrip(".")
    if cascade.is_soft_404(page) or cascade.is_dead_site(page):
        identity = "rechazada"
    elif authority.registry_official_host(municipio, host_final):
        identity = "confirmada"
    else:
        identity = cascade._candidate_identity_state(page, municipio, ())
        # Mision D (12-jul), regla (b) -- universo (site_base): escape hatch
        # SOLO para subir a 'confirmada'; nunca baja lo que cascade ya dijo.
        # Exige DOS hechos independientes (site_base match + mencion literal
        # del municipio en el contenido) -- ver docstring de
        # ``universe_identity_confirms``. Cubre los casos donde cascade
        # rechaza por slug (conector da/de/do/das/dos retenido en el host, o
        # abreviatura no estandar como pmfv/pmgentil) antes de leer contenido.
        if identity != "confirmada" and authority.universe_identity_confirms(
            municipio, final_url, page,
        ):
            identity = "confirmada"

    provenance_total = (
        tuple(provenance)
        + authority.registry_provenance(municipio, final_url)
        # Mision D (12-jul), reglas (a)/(b): fuentes de autoridad nuevas para
        # municipios NUEVOS (fuera del registro curado). Ninguna de las dos
        # alimenta identidad por si sola -- ver los docstrings de cada
        # funcion y el comentario "provenance de REDIRECT" arriba: el mismo
        # principio aplica aqui.
        + authority.delegated_platform_provenance(municipio, final_url, page)
        + authority.universe_provenance(municipio, final_url)
    )
    source_kind, authority_state = cascade._candidate_source_and_authority(
        page, municipio, provenance_total, identity, source,
    )

    canonical = cascade._canonical_bucket(bucket) or bucket
    bucket_hint = (
        "concursos" if canonical == "concurso_publico"
        else "processos" if canonical == "processo_seletivo"
        else canonical
    )
    candidate_id = cascade._candidate_record_id(
        final_url=final_url, source=source, tier=tier,
        municipio=municipio, bucket=canonical, snapshot=evidence,
    )
    return cascade.CandidateRecord(
        candidate_id=candidate_id,
        requested_url=requested,
        final_url=final_url,
        source=source,
        tier=tier,
        municipio=municipio,
        bucket_hint=bucket_hint,
        evidence_snapshot=evidence,
        authority=authority_state,
        identity=identity,
        page_role=NO_V1_ADJUDICATION,
        evidence_state=evidence_state,
        bucket=canonical,
        decision=NO_V1_ADJUDICATION,
        reason="structural_evidence_only_v2",
        source_kind=source_kind,
        accessible=accessible,
    )
