"""Offline contract tests for the isolated F3 Multi24 adapter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

from scripts.fase2_municipios.v2.eval.f3_multi24_adapter import (
    Multi24Authority,
    Multi24ContractError,
    Multi24Snapshot,
    VALID_DISPOSITIONS,
    analyze_multi24 as _analyze_multi24,
    decode_snapshot,
)


pytestmark = pytest.mark.offline

FIXTURES = Path(__file__).parent / "fixtures" / "f3_multi24"
PROGRESSO_ENTRY = (
    "https://sistemas.progresso.rs.gov.br/multi24/sistemas/transparencia/index"
    "?entidade=1&secao=dinamico&id=6146"
)
FLORES_ENTRY = (
    "https://pmfloresdacunha.multi24h.com.br/multi24/sistemas/transparencia/"
    "?entidade=1&secao=dinamico&id=13600"
)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def without_sanitized_identity(body: bytes, municipio: str) -> bytes:
    text = body.decode("utf-8")
    text = re.sub(
        r"<title>.*?</title>",
        "<title>Portal da Transparência</title>",
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = text.replace(f"<p>Município de {municipio}</p>", "", 1)
    return text.encode("utf-8")


def with_live_identity_banner(body: bytes, municipio: str) -> bytes:
    text = without_sanitized_identity(body, municipio).decode("utf-8")
    banner = (
        '<div class="row background-nome"><div class="col-md-12">'
        f'<p class="cidade-centro">Município de {municipio}</p>'
        "</div></div>"
    )
    return text.replace("<body>", f"<body>{banner}", 1).encode("utf-8")


def authority_for(url: str, *, municipio: str | None = None) -> Multi24Authority:
    parsed = urlsplit(url)
    if "floresdacunha" in parsed.netloc:
        default_municipio = "Flores da Cunha"
        source_url = "https://authority.fixture.test/flores-da-cunha/portal-navigation"
    else:
        default_municipio = "Progresso"
        source_url = "https://authority.fixture.test/progresso/portal-navigation"
    municipio = municipio or default_municipio
    escaped_target = url.replace("&", "&amp;")
    body = (
        f"<html><head><title>Município de {municipio}</title></head>"
        f"<body><header><p>Município de {municipio}</p></header>"
        f'<a href="{escaped_target}">Portal da Transparência</a></body></html>'
    ).encode("utf-8")
    proof = Multi24Snapshot(
        requested_url=source_url,
        final_url=source_url,
        status_code=200,
        body=body,
        content_type="text/html; charset=utf-8",
    )
    return Multi24Authority(
        official_source_origins=("https://authority.fixture.test",),
        navigation_snapshots=(proof,),
    )


def authority_with_target(
    target_url: str,
    *,
    municipio: str = "Flores da Cunha",
    label: str = "Acesso com Senha",
) -> Multi24Authority:
    source_url = "https://official.fixture.test/"
    escaped_target = target_url.replace("&", "&amp;")
    body = (
        f"<html><head><title>Prefeitura Municipal de {municipio} - Home</title></head>"
        f'<body><a href="{escaped_target}">{label}</a></body></html>'
    ).encode("utf-8")
    proof = Multi24Snapshot(
        requested_url=source_url,
        final_url=source_url,
        status_code=200,
        body=body,
        content_type="text/html; charset=utf-8",
    )
    return Multi24Authority(
        official_source_origins=("https://official.fixture.test",),
        navigation_snapshots=(proof,),
    )


def analyze_multi24(**kwargs):
    entry = kwargs["entry"]
    return _analyze_multi24(authority=authority_for(entry.requested_url), **kwargs)


def snapshot(
    url: str,
    fixture: str,
    *,
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
) -> Multi24Snapshot:
    return Multi24Snapshot(
        requested_url=url,
        final_url=url,
        status_code=status_code,
        body=fixture_bytes(fixture),
        content_type=content_type,
        retrieved_at="2026-07-13T00:00:00Z",
    )


def entry_edges(entry: Multi24Snapshot, municipio: str) -> tuple:
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio=municipio,
        bucket="concurso_publico",
        current_year=2026,
    )
    return result.edges


def target_for(entry: Multi24Snapshot, municipio: str, label: str, branch: str) -> str:
    matches = [
        edge.target_url
        for edge in entry_edges(entry, municipio)
        if edge.label == label and branch in edge.provenance
    ]
    assert len(matches) == 1
    return matches[0]


def test_progresso_follows_real_child_link_and_requires_child_items() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    child = snapshot(target, "progresso_2026.html")

    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "candidata"
    assert result.reason == "candidate_nodes_with_item_evidence"
    assert result.index_url == PROGRESSO_ENTRY
    assert [candidate.label for candidate in result.candidates] == ["2026"]
    assert result.candidates[0].provenance == ("Concursos Públicos", "2026")
    assert len(result.candidates[0].items) == 3
    assert all("2026" in item.title for item in result.candidates[0].items)
    assert any(value.startswith("official_source_sha256:") for value in result.authority_evidence)
    assert any(value.startswith("navigation_target_origin:") for value in result.authority_evidence)


def test_historical_content_on_entry_does_not_validate_missing_2026_child() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert result.index_url == ""
    assert result.reason == "linked_current_year_page_missing"
    assert result.items == ()


def test_cultural_2026_and_pss_are_not_promoted_to_public_competition() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: snapshot(target, "progresso_2026.html")},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert len(result.candidates) == 1
    assert "Concursos Culturais" not in result.candidates[0].provenance
    assert all("PSS" not in candidate.label for candidate in result.candidates)


def test_html_ampersands_are_decoded_once_and_ids_only_come_from_hrefs() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.edges
    assert all("&amp;" not in edge.target_url for edge in result.edges)
    assert all(parse_qs(urlsplit(edge.target_url).query).get("id") for edge in result.edges)
    raw_fixture = fixture_bytes("progresso_tree.html").decode("utf-8")
    ids_in_fixture = {
        value
        for value in re.findall(r"(?:&amp;|&)id=(\d+)", raw_fixture)
    }
    ids_returned = {parse_qs(urlsplit(edge.target_url).query)["id"][0] for edge in result.edges}
    assert ids_returned <= ids_in_fixture


def test_unlinked_orphan_snapshot_is_ignored_even_when_it_has_valid_content() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    orphan_url = (
        "https://sistemas.progresso.rs.gov.br/multi24/sistemas/transparencia/index"
        "?entidade=1&secao=dinamico&id=99999"
    )
    orphan = snapshot(orphan_url, "progresso_2026.html")
    result = analyze_multi24(
        entry=entry,
        linked_pages={orphan_url: orphan},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert orphan_url not in dict(result.raw_sha256_by_url)
    assert result.active_node_urls == ()


def test_linked_page_identity_mismatch_fails_closed() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    wrong_body = fixture_bytes("progresso_2026.html").replace(b"Progresso", b"Outra Cidade")
    child = replace(snapshot(target, "progresso_2026.html"), body=wrong_body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_identity_mismatch" in result.review_reasons


def test_linked_year_label_without_two_numbered_items_stays_review() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_2026.html")
    body = body.replace(b"Retifica\xc3\xa7\xc3\xa3o 02/2026", b"Aviso geral")
    body = body.replace(b"Homologa\xc3\xa7\xc3\xa3o 03/2026", b"Calendario")
    child = replace(snapshot(target, "progresso_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


def test_flores_variable_depth_keeps_cp_separate_from_pss_and_psp() -> None:
    entry = snapshot(FLORES_ENTRY, "flores_tree.html")
    cp_target = target_for(entry, "Flores da Cunha", "Concurso Público 01/2026", "Concursos Públicos")
    pss_target = target_for(entry, "Flores da Cunha", "PSS 01/2026", "Processo Seletivo Simplificado")
    psp_target = target_for(entry, "Flores da Cunha", "PSP 01/2026", "Processo Seletivo Público")
    pages = {
        cp_target: snapshot(cp_target, "flores_cp_2026.html"),
        pss_target: snapshot(pss_target, "flores_pss_01_2026.html"),
        psp_target: snapshot(psp_target, "flores_psp_01_2026.html"),
    }

    cp_result = analyze_multi24(
        entry=entry,
        linked_pages=pages,
        municipio="Flores da Cunha",
        bucket="concurso_publico",
        current_year=2026,
    )
    pss_result = analyze_multi24(
        entry=entry,
        linked_pages=pages,
        municipio="Flores da Cunha",
        bucket="processo_seletivo",
        current_year=2026,
    )

    assert [candidate.label for candidate in cp_result.candidates] == ["Concurso Público 01/2026"]
    assert {candidate.label for candidate in pss_result.candidates} == {"PSS 01/2026", "PSP 01/2026"}
    assert all(candidate.bucket == "concurso_publico" for candidate in cp_result.candidates)
    assert all(candidate.bucket == "processo_seletivo" for candidate in pss_result.candidates)


def test_linked_entidade_is_preserved_instead_of_assuming_one() -> None:
    source = fixture_bytes("progresso_tree.html").replace(b"entidade=1", b"entidade=7")
    entry_url = PROGRESSO_ENTRY.replace("entidade=1", "entidade=7")
    entry = replace(snapshot(entry_url, "progresso_tree.html"), body=source)
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    child = snapshot(target, "progresso_2026.html")
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "candidata"
    assert parse_qs(urlsplit(result.active_node_urls[0]).query)["entidade"] == ["7"]


def test_destination_redirect_cannot_change_opaque_id_or_entidade() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    child = snapshot(target, "progresso_2026.html")
    parsed = urlsplit(target)
    changed_query = parse_qs(parsed.query)
    changed_query["id"] = ["99999"]
    changed_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(changed_query, doseq=True), "")
    )
    changed = replace(child, final_url=changed_url)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: changed},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_destination_mismatch" in result.review_reasons


def test_utf8_body_overrides_wrong_latin1_http_declaration() -> None:
    body = fixture_bytes("progresso_tree.html")
    snap = replace(
        snapshot(PROGRESSO_ENTRY, "progresso_tree.html"),
        body=body,
        content_type="text/html; charset=iso-8859-1",
    )
    decoded = decode_snapshot(snap)
    assert "Município de Progresso" in decoded.text
    assert decoded.charset_used == "utf-8"
    assert decoded.header_mismatch is True
    assert decoded.meta_mismatch is False


def test_real_latin1_body_wins_over_false_utf8_meta() -> None:
    html = (
        '<html><head><meta charset="UTF-8"></head><body>'
        "Portal da Transparência - Município de São Vendelino"
        "</body></html>"
    )
    snap = Multi24Snapshot(
        requested_url="https://example.test/multi24/sistemas/transparencia/?secao=dinamico&id=1",
        final_url="https://example.test/multi24/sistemas/transparencia/?secao=dinamico&id=1",
        status_code=200,
        body=html.encode("iso-8859-1"),
        content_type="text/html; charset=iso-8859-1",
    )
    decoded = decode_snapshot(snap)
    assert "Município de São Vendelino" in decoded.text
    assert decoded.charset_used == "iso-8859-1"
    assert decoded.header_mismatch is False
    assert decoded.meta_mismatch is True


def test_invalid_contract_raises_but_evidence_failure_returns_review() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    with pytest.raises(Multi24ContractError, match="invalid_bucket"):
        analyze_multi24(
            entry=entry,
            linked_pages={},
            municipio="Progresso",
            bucket="ambos",
            current_year=2026,
        )

    failed = analyze_multi24(
        entry=replace(entry, status_code=500),
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert failed.disposition == "revisar"
    assert failed.reason == "entry_status_not_200"


def test_adapter_can_never_emit_confirmado() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: snapshot(target, "progresso_2026.html")},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert VALID_DISPOSITIONS == {"candidata", "revisar"}
    assert result.disposition in VALID_DISPOSITIONS
    assert result.disposition != "confirmado"


def test_entry_origin_must_be_explicitly_authorized_with_navigation_evidence() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    unauthorized = authority_for(
        PROGRESSO_ENTRY.replace("sistemas.progresso.rs.gov.br", "portal-impostor.example")
    )
    result = _analyze_multi24(
        entry=entry,
        linked_pages={},
        authority=unauthorized,
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert result.reason == "official_navigation_link_not_proven"

    with pytest.raises(Multi24ContractError, match="official_navigation_evidence_required"):
        _analyze_multi24(
            entry=entry,
            linked_pages={},
            authority=Multi24Authority(
                official_source_origins=("https://authority.fixture.test",),
                navigation_snapshots=(),
            ),
            municipio="Progresso",
            bucket="concurso_publico",
            current_year=2026,
        )


def test_official_title_and_exact_same_origin_portal_root_prove_delegation() -> None:
    entry = snapshot(FLORES_ENTRY, "flores_tree.html")
    target = target_for(
        entry,
        "Flores da Cunha",
        "Concurso Público 01/2026",
        "Concursos Públicos",
    )
    portal_root = (
        "https://pmfloresdacunha.multi24h.com.br/"
        "multi24/sistemas/portal/#tab-login"
    )

    result = _analyze_multi24(
        entry=entry,
        linked_pages={target: snapshot(target, "flores_cp_2026.html")},
        authority=authority_with_target(portal_root),
        municipio="Flores da Cunha",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "candidata"
    assert (
        "official_navigation_relation:same_origin_multi24_portal_root"
        in result.authority_evidence
    )
    assert f"official_navigation_target:{portal_root}" in result.authority_evidence


@pytest.mark.parametrize(
    ("target_url", "label"),
    [
        (
            "https://floresdacunha.multi24h.com.br/multi24/sistemas/portal/",
            "Acesso com Senha",
        ),
        (
            "https://pmfloresdacunha.multi24h.com.br/multi24/sistemas/portal-malicioso/",
            "Portal da Transparência",
        ),
        (
            "https://pmfloresdacunha.multi24h.com.br/multi24/sistemas/portal/extra",
            "Concurso",
        ),
        (
            "https://pmfloresdacunha.multi24h.com.br/multi24/sistemas/portal/?atalho=x",
            "Acesso com Senha",
        ),
        (
            "https://pmfloresdacunha.multi24h.com.br/arbitrary",
            "Concursos/Processos Seletivos",
        ),
        (
            "http://pmfloresdacunha.multi24h.com.br/multi24/sistemas/portal/",
            "Acesso com Senha",
        ),
        (
            "https://user@pmfloresdacunha.multi24h.com.br/multi24/sistemas/portal/",
            "Acesso com Senha",
        ),
        (
            "https://pmfloresdacunha.multi24h.com.br:444/multi24/sistemas/portal/",
            "Acesso com Senha",
        ),
    ],
)
def test_portal_root_delegation_rejects_broad_or_ambiguous_targets(
    target_url: str,
    label: str,
) -> None:
    entry = snapshot(FLORES_ENTRY, "flores_tree.html")
    result = _analyze_multi24(
        entry=entry,
        linked_pages={},
        authority=authority_with_target(target_url, label=label),
        municipio="Flores da Cunha",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "revisar"
    assert result.reason == "official_navigation_link_not_proven"
    assert result.authority_evidence == ()


def test_municipality_identity_does_not_accept_a_longer_name_with_same_prefix() -> None:
    original = fixture_bytes("progresso_tree.html")
    body = original.replace("Progresso".encode(), "São José do Norte".encode())
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)
    result = _analyze_multi24(
        entry=entry,
        linked_pages={},
        authority=authority_for(PROGRESSO_ENTRY, municipio="São José"),
        municipio="São José",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert result.reason == "entry_identity_mismatch"


def test_contradictory_title_cannot_be_overridden_by_expected_header_mention() -> None:
    body = fixture_bytes("progresso_tree.html").replace(
        b"Portal da Transpar\xc3\xaancia - Munic\xc3\xadpio de Progresso",
        b"Portal da Transpar\xc3\xaancia - Munic\xc3\xadpio de Outra Cidade",
        1,
    )
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert result.reason == "entry_identity_mismatch"


def test_live_multi24_banner_identity_is_accepted_for_entry_and_child() -> None:
    entry = replace(
        snapshot(PROGRESSO_ENTRY, "progresso_tree.html"),
        body=with_live_identity_banner(fixture_bytes("progresso_tree.html"), "Progresso"),
    )
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    child = snapshot(target, "progresso_live_2026.html")

    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "candidata"
    assert result.reason == "candidate_nodes_with_item_evidence"
    assert result.identity_evidence == ("municipio:progresso",)
    assert len(result.items) == 8
    assert all("secao=download" in item.url for item in result.items)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_role",
        "wrong_heading_class",
        "hidden_path",
        "wrong_year",
        "wrong_bucket",
        "contradictory_duplicate",
    ],
)
def test_live_multi24_path_surface_remains_exact_and_fail_closed(mutation: str) -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_live_2026.html")
    if mutation == "wrong_role":
        body = body.replace(b'role="main"', b'role="region"', 1)
    elif mutation == "wrong_heading_class":
        body = body.replace(b'<h1 class="title">', b'<h1 class="page-title">', 1)
    elif mutation == "hidden_path":
        body = body.replace(b'role="main">', b'role="main" hidden>', 1)
    elif mutation == "wrong_year":
        body = body.replace(
            b"<h1 class=\"title\">Concursos P\xc3\xbablicos / 2026</h1>",
            b"<h1 class=\"title\">Concursos P\xc3\xbablicos / 2025</h1>",
            1,
        )
    elif mutation == "wrong_bucket":
        body = body.replace(
            b"<h1 class=\"title\">Concursos P\xc3\xbablicos / 2026</h1>",
            b"<h1 class=\"title\">Processos Seletivos / 2026</h1>",
            1,
        )
    else:
        body = body.replace(
            b"<h1 class=\"title\">Concursos P\xc3\xbablicos / 2026</h1>",
            (
                b"<h1 class=\"title\">Concursos P\xc3\xbablicos / 2026</h1>"
                b"<h1 class=\"title\">Concursos P\xc3\xbablicos / 2025</h1>"
            ),
            1,
        )
    child = replace(
        snapshot(target, "progresso_live_2026.html"),
        body=body,
    )

    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "revisar"
    assert "linked_page_breadcrumb_mismatch" in result.review_reasons


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_node_span",
        "wrong_section",
        "wrong_sub",
        "wrong_entity",
        "missing_publication_class",
        "wrong_publication_year",
        "impossible_publication_date",
        "excluded_titles",
    ],
)
def test_live_download_rows_reject_incomplete_or_ambiguous_evidence(mutation: str) -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_live_2026.html")
    if mutation == "wrong_node_span":
        body = body.replace(b'id="span11730"', b'id="span99999"')
    elif mutation == "wrong_section":
        body = body.replace(b"secao=download", b"secao=dinamico")
    elif mutation == "wrong_sub":
        body = body.replace(b"sub=menu", b"sub=other")
    elif mutation == "wrong_entity":
        body = body.replace(b"entidade=1&amp;secao=download", b"entidade=2&amp;secao=download")
    elif mutation == "missing_publication_class":
        body = body.replace(b'class="publicacao"', b'class="published"')
    elif mutation == "wrong_publication_year":
        body = re.sub(br"(Publicado em \d{2}/\d{2}/)2026", br"\g<1>2025", body)
    elif mutation == "impossible_publication_date":
        body = re.sub(
            br"Publicado em \d{2}/\d{2}/2026",
            b"Publicado em 31/02/2026",
            body,
        )
    else:
        body = body.replace(b"CP PROGRESSO", b"Concurso Cultural PROGRESSO")
        body = body.replace(b"Progresso CP", b"Concurso Cultural Progresso")
    child = replace(
        snapshot(target, "progresso_live_2026.html"),
        body=body,
    )

    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


def test_live_download_rows_dedupe_by_document_id_and_still_require_two() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    original = fixture_bytes("progresso_live_2026.html")
    duplicate = original.replace(b"id=11945", b"id=11946", 1)
    duplicate_result = analyze_multi24(
        entry=entry,
        linked_pages={
            target: replace(
                snapshot(target, "progresso_live_2026.html"),
                body=duplicate,
            )
        },
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert duplicate_result.disposition == "candidata"
    assert len(duplicate_result.items) == 7

    one_valid = re.sub(
        br'id="span11730"',
        b'id="span99999"',
        original,
        count=7,
    )
    one_result = analyze_multi24(
        entry=entry,
        linked_pages={
            target: replace(
                snapshot(target, "progresso_live_2026.html"),
                body=one_valid,
            )
        },
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert one_result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in one_result.review_reasons


@pytest.mark.parametrize(
    "identity_markup",
    [
        "<main><p>Município de Progresso</p></main>",
        "<main><article><h1>Edital da Prefeitura Municipal de Progresso</h1></article></main>",
        "<aside><h2>Município de Progresso</h2></aside>",
        "<nav><h2>Município de Progresso</h2></nav>",
        '<p class="cidade-centro">Município de Progresso</p>',
        '<div class="background-nome"><p>Município de Progresso</p></div>',
        (
            '<nav><div class="row background-nome"><div class="col-md-12">'
            '<p class="cidade-centro">Município de Progresso</p>'
            "</div></div></nav>"
        ),
        (
            '<div class="row background-nome" hidden><div class="col-md-12">'
            '<p class="cidade-centro">Município de Progresso</p>'
            "</div></div>"
        ),
        (
            '<div class="row background-nome" aria-hidden="true"><div class="col-md-12">'
            '<p class="cidade-centro">Município de Progresso</p>'
            "</div></div>"
        ),
        (
            '<div class="row background-nome"><div class="col-md-12">'
            '<select><option class="cidade-centro">Município de Progresso</option></select>'
            "</div></div>"
        ),
        "<footer><p>Prefeitura Municipal de Progresso</p></footer>",
    ],
)
def test_non_authoritative_identity_mentions_remain_fail_closed(identity_markup: str) -> None:
    text = without_sanitized_identity(
        fixture_bytes("progresso_tree.html"),
        "Progresso",
    ).decode("utf-8")
    body = text.replace("<body>", f"<body>{identity_markup}", 1).encode("utf-8")
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)

    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "revisar"
    assert result.reason == "entry_identity_mismatch"
    assert result.identity_evidence == ()


def test_title_declaration_alone_is_not_multi24_identity() -> None:
    text = without_sanitized_identity(
        fixture_bytes("progresso_tree.html"),
        "Progresso",
    ).decode("utf-8")
    body = text.replace(
        "<title>Portal da Transparência</title>",
        "<title>Município de Progresso</title>",
        1,
    ).encode("utf-8")
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)

    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "revisar"
    assert result.reason == "entry_identity_mismatch"
    assert result.identity_evidence == ()


def test_incidental_other_city_in_content_does_not_contradict_live_banner() -> None:
    body = with_live_identity_banner(
        fixture_bytes("progresso_tree.html"),
        "Progresso",
    ).replace(
        b"<main>",
        b"<main><article><h2>Prefeitura Municipal de Outra Cidade</h2></article>",
        1,
    )
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)

    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.reason == "linked_current_year_page_missing"
    assert result.identity_evidence == ("municipio:progresso",)


def test_live_banner_cannot_override_a_contradictory_title() -> None:
    body = with_live_identity_banner(
        fixture_bytes("progresso_tree.html"),
        "Progresso",
    ).replace(
        b"<title>Portal da Transpar\xc3\xaancia</title>",
        b"<title>Munic\xc3\xadpio de Outra Cidade</title>",
        1,
    )
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)

    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )

    assert result.disposition == "revisar"
    assert result.reason == "entry_identity_mismatch"
    assert result.identity_evidence == ()


def test_hyphenated_municipality_identity_is_preserved() -> None:
    body = fixture_bytes("progresso_tree.html").replace(
        b"Progresso",
        "Não-Me-Toque".encode("utf-8"),
    )
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)
    result = _analyze_multi24(
        entry=entry,
        linked_pages={},
        authority=authority_for(PROGRESSO_ENTRY, municipio="Não-Me-Toque"),
        municipio="Não-Me-Toque",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.reason == "linked_current_year_page_missing"
    assert result.identity_evidence == ("municipio:nao-me-toque",)


def test_specific_leaf_must_match_breadcrumb_not_just_bucket_and_year() -> None:
    entry = snapshot(FLORES_ENTRY, "flores_tree.html")
    target = target_for(entry, "Flores da Cunha", "Concurso Público 01/2026", "Concursos Públicos")
    wrong_leaf = fixture_bytes("flores_cp_2026.html").replace(b"01/2026", b"02/2026")
    child = replace(snapshot(target, "flores_cp_2026.html"), body=wrong_leaf)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Flores da Cunha",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_breadcrumb_mismatch" in result.review_reasons


def test_specific_leaf_rejects_suffix_variant_of_same_number_and_year() -> None:
    entry = snapshot(FLORES_ENTRY, "flores_tree.html")
    target = target_for(entry, "Flores da Cunha", "Concurso Público 01/2026", "Concursos Públicos")
    body = fixture_bytes("flores_cp_2026.html").replace(
        b"Concursos P\xc3\xbablicos / Concurso P\xc3\xbablico 01/2026</nav>",
        b"Concursos P\xc3\xbablicos / Concurso P\xc3\xbablico 01/2026-A</nav>",
    )
    child = replace(snapshot(target, "flores_cp_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Flores da Cunha",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_breadcrumb_mismatch" in result.review_reasons


def test_one_document_nested_in_a_list_row_counts_only_once() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_2026.html")
    one_item = (
        b'<main id="conteudo"><ul><li>Documento oficial '
        b'<a href="/documentos/edital-01-2026.pdf">Edital 01/2026</a>'
        b'</li></ul></main>'
    )
    body = re.sub(br'<main id="conteudo">.*?</main>', one_item, body, flags=re.DOTALL)
    child = replace(snapshot(target, "progresso_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


def test_same_document_with_tracking_variants_counts_only_once() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_2026.html")
    duplicate_links = (
        b'<main id="conteudo">'
        b'<a href="/docs/edital.pdf?cache=one">Edital 01/2026</a>'
        b'<a href="/docs/edital.pdf?cache=two">Baixar Edital 01/2026</a>'
        b'</main>'
    )
    body = re.sub(br'<main id="conteudo">.*?</main>', duplicate_links, body, flags=re.DOTALL)
    child = replace(snapshot(target, "progresso_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


def test_opposite_bucket_and_cultural_items_do_not_validate_cp_page() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_2026.html")
    bad_items = (
        b'<main id="conteudo">'
        b'<a href="/docs/pss-01-2026.pdf">Edital PSS 01/2026</a>'
        b'<a href="/docs/cultural-02-2026.pdf">Resultado Concurso Cultural 02/2026</a>'
        b'</main>'
    )
    body = re.sub(br'<main id="conteudo">.*?</main>', bad_items, body, flags=re.DOTALL)
    child = replace(snapshot(target, "progresso_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


def test_aprovacao_does_not_match_the_word_prova() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_2026.html")
    false_terms = (
        b'<main id="conteudo">'
        b'<a href="/docs/aprovacao-01-2026.pdf">Aprovacao 01/2026</a>'
        b'<a href="/docs/aprovacao-02-2026.pdf">Aprovacao 02/2026</a>'
        b'</main>'
    )
    body = re.sub(br'<main id="conteudo">.*?</main>', false_terms, body, flags=re.DOTALL)
    child = replace(snapshot(target, "progresso_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


def test_content_root_marked_as_menu_cannot_supply_item_evidence() -> None:
    entry = snapshot(PROGRESSO_ENTRY, "progresso_tree.html")
    target = target_for(entry, "Progresso", "2026", "Concursos Públicos")
    body = fixture_bytes("progresso_2026.html")
    fake_menu = (
        b'<main id="menu">'
        b'<a href="/docs/edital-01-2026.pdf">Edital 01/2026</a>'
        b'<a href="/docs/resultado-02-2026.pdf">Resultado 02/2026</a>'
        b'</main>'
    )
    body = re.sub(br'<main id="conteudo">.*?</main>', fake_menu, body, flags=re.DOTALL)
    child = replace(snapshot(target, "progresso_2026.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={target: child},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert "linked_page_insufficient_item_evidence" in result.review_reasons


@pytest.mark.parametrize(
    "excluded_label",
    ["Chamadas Públicas e Concursos Públicos", "Licitações e Concursos Públicos"],
)
def test_plural_excluded_branches_cannot_become_cp(excluded_label: str) -> None:
    body = fixture_bytes("progresso_tree.html")
    body = re.sub(
        br'<li><a class="link_menu" href="\?entidade=1&amp;secao=dinamico&amp;id=11730">2026</a></li>',
        b"",
        body,
    )
    body = body.replace(
        "Chamadas Públicas e Concursos Culturais".encode(),
        excluded_label.encode(),
    )
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.reason == "no_linked_current_year_node"


def test_dynamic_link_outside_verified_menu_is_not_a_tree_edge() -> None:
    body = fixture_bytes("progresso_tree.html")
    body = re.sub(
        br'<li><a class="link_menu" href="\?entidade=1&amp;secao=dinamico&amp;id=11730">2026</a></li>',
        b"",
        body,
    )
    injected = (
        b'<a class="not_link_menu" href="?entidade=1&amp;secao=dinamico&amp;id=77777">'
        b'Concurso Publico 01/2026</a></main>'
    )
    body = body.replace(b"</main>", injected)
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.reason == "no_linked_current_year_node"
    assert all("77777" not in edge.target_url for edge in result.edges)


def test_not_link_menu_class_is_not_a_platform_signature() -> None:
    body = fixture_bytes("progresso_tree.html")
    body = body.replace(b"link_menu", b"not_link_menu")
    body = body.replace("Mapa do Portal".encode(), b"Navegacao geral")
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), body=body)
    result = analyze_multi24(
        entry=entry,
        linked_pages={},
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.reason == "entry_platform_signature_missing"


@pytest.mark.parametrize(
    "bad_url",
    [
        PROGRESSO_ENTRY.replace("/transparencia/index", "/transparencia-falso"),
        PROGRESSO_ENTRY.replace("/multi24/", "/evil/multi24/"),
        PROGRESSO_ENTRY.replace("sistemas.progresso.rs.gov.br", "sistemas.progresso.rs.gov.br:bad"),
    ],
)
def test_false_multi24_path_and_malformed_port_fail_closed(bad_url: str) -> None:
    entry = replace(snapshot(PROGRESSO_ENTRY, "progresso_tree.html"), requested_url=bad_url, final_url=bad_url)
    result = _analyze_multi24(
        entry=entry,
        linked_pages={},
        authority=authority_for(PROGRESSO_ENTRY),
        municipio="Progresso",
        bucket="concurso_publico",
        current_year=2026,
    )
    assert result.disposition == "revisar"
    assert result.reason in {"entry_url_not_multi24", "entry_origin_not_authorized"}


def test_reversible_mojibake_is_repaired_once_and_recorded() -> None:
    healthy = (
        '<html><head><meta charset="UTF-8"></head>'
        '<body><p>Município de Progresso</p></body></html>'
    )
    mojibake = healthy.encode("utf-8").decode("iso-8859-1")
    snap = Multi24Snapshot(
        requested_url=PROGRESSO_ENTRY,
        final_url=PROGRESSO_ENTRY,
        status_code=200,
        body=mojibake.encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )
    decoded = decode_snapshot(snap)
    assert "Município de Progresso" in decoded.text
    assert decoded.mojibake_repaired is True

    healthy_snap = replace(snap, body=decoded.text.encode("utf-8"))
    decoded_again = decode_snapshot(healthy_snap)
    assert decoded_again.text == decoded.text
    assert decoded_again.mojibake_repaired is False


def test_cp1252_mojibake_is_repaired_once() -> None:
    healthy = "<html><body><p>Seleção – Município de Progresso</p></body></html>"
    mojibake = healthy.encode("utf-8").decode("windows-1252")
    snap = Multi24Snapshot(
        requested_url=PROGRESSO_ENTRY,
        final_url=PROGRESSO_ENTRY,
        status_code=200,
        body=mojibake.encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )
    decoded = decode_snapshot(snap)
    assert "Seleção – Município de Progresso" in decoded.text
    assert decoded.mojibake_repaired is True
