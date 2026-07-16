from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fase2_municipios.v2.eval.f3_atende_adapter import (
    derive_service_detail_url,
    detect_atende_shell,
    parse_plugin_portal_response,
    plan_playwright,
    propose_candidates,
)


FIXTURES = Path(__file__).parent / "fixtures" / "f3_atende"
PAGE_URL = "https://fixture.atende.net/cidadao/pagina/processos-seletivos-2026"
SERVICE_URL = "https://fixture.atende.net/autoatendimento/servicos/editais-de-concursos-e-processos"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _shell() -> str:
    return _read("atende_plugin_shell.html")


def _payload():
    return json.loads(_read("plugin_portal_xhr.json"))


def test_detects_atende_shell_but_not_normal_content_page() -> None:
    assert detect_atende_shell(PAGE_URL, _shell())
    assert not detect_atende_shell("https://prefeitura.example/noticias", _read("normal_page.html"))
    assert not detect_atende_shell(PAGE_URL, _read("normal_page.html"))


def test_parses_item_positive_rows_from_plugin_portal_json_fixture() -> None:
    items = parse_plugin_portal_response(
        _payload(), response_url="https://fixture.atende.net/xhr/plugin/fixture"
    )
    assert [item.title for item in items] == [
        "Processo Seletivo Simplificado 01/2026",
        "Processo Seletivo Simplificado no 02/2026",
        "Processo Seletivo Simplificado no 03/2026",
    ]
    assert all("/fixture-documentos/" in item.document_url for item in items)
    assert len({item.response_sha256 for item in items}) == 1


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (SERVICE_URL, SERVICE_URL + "/detalhar/1"),
        (SERVICE_URL + "/", SERVICE_URL + "/detalhar/1"),
        (SERVICE_URL + "/detalhar/1", SERVICE_URL + "/detalhar/1"),
        (SERVICE_URL + "?lang=pt", SERVICE_URL + "/detalhar/1?lang=pt"),
    ],
)
def test_derives_detail_one_from_any_service_slug(source: str, expected: str) -> None:
    assert derive_service_detail_url(source) == expected


def test_non_atende_page_never_proposes_even_with_injected_rows() -> None:
    assert propose_candidates(
        "https://prefeitura.example/noticias",
        page_html=_read("normal_page.html"),
        plugin_response=_payload(),
        plugin_response_url="https://fixture.atende.net/xhr/plugin/fixture",
        rendered_html=_read("plugin_portal_rendered.html"),
    ) == []


def test_shell_or_unmaterialized_xhr_never_proposes() -> None:
    assert propose_candidates(PAGE_URL, page_html=_shell()) == []
    assert propose_candidates(
        PAGE_URL,
        page_html=_shell(),
        plugin_response=_payload(),
        plugin_response_url="https://fixture.atende.net/xhr/plugin/fixture",
        rendered_html="<div id='PluginPortal_65'>Por favor, aguarde...</div>",
    ) == []
    assert propose_candidates(
        PAGE_URL,
        page_html=_shell(),
        plugin_response=_payload(),
        plugin_response_url="https://unrelated.example/xhr",
        rendered_html=_read("plugin_portal_rendered.html"),
    ) == []


def test_page_mode_proposes_canonical_page_with_provenance_and_never_confirms() -> None:
    proposals = propose_candidates(
        PAGE_URL,
        page_html=_shell(),
        plugin_response=_payload(),
        plugin_response_url="https://fixture.atende.net/xhr/plugin/fixture",
        rendered_html=_read("plugin_portal_rendered.html"),
    )
    assert len(proposals) == 3
    proposal = proposals[0]
    assert proposal["url_candidata"] == PAGE_URL
    assert proposal["source_url"] == PAGE_URL
    assert proposal["mode"] == "pagina_plugin_portal"
    assert [state["state"] for state in proposal["provenance"]] == ["shell", "xhr_response", "rendered"]
    assert proposal["disposition"] == "propose"
    assert proposal["confirmed"] is False
    assert proposal["evidence"]["item_positive"] is True


def test_service_iframe_mode_proposes_service_and_never_confirms() -> None:
    capture = {
        "detail_url": SERVICE_URL + "/detalhar/1",
        "iframe_src": "https://fixture.atende.net/frame/publico",
        "response_url": "https://fixture.atende.net/frame/api/lista",
        "rendered_html": _read("plugin_portal_rendered.html"),
        "rows": _payload(),
    }
    proposals = propose_candidates(SERVICE_URL, page_html=_shell(), iframe_capture=capture)
    assert len(proposals) == 3
    proposal = proposals[0]
    assert proposal["url_candidata"] == SERVICE_URL
    assert proposal["source_url"] == SERVICE_URL
    assert proposal["mode"] == "servicio_iframe"
    assert [state["state"] for state in proposal["provenance"]] == [
        "service", "detail", "iframe", "frame_response"
    ]
    assert proposal["disposition"] == "propose"
    assert proposal["confirmed"] is False


def test_playwright_plans_use_fresh_session_and_do_not_guess_endpoint() -> None:
    page_plan = plan_playwright(PAGE_URL, page_html=_shell())
    service_plan = plan_playwright(SERVICE_URL, page_html=_shell())
    assert page_plan is not None and service_plan is not None
    assert page_plan["session"]["new_public_context"] is True
    assert page_plan["session"]["import_storage_state"] is False
    assert "do not reuse a plugin id or endpoint" in page_plan["request_contract"]
    assert service_plan["detail_url"] == SERVICE_URL + "/detalhar/1"


def test_every_success_path_is_explicitly_proposal_only() -> None:
    page = propose_candidates(
        PAGE_URL,
        page_html=_shell(),
        plugin_response=_payload(),
        plugin_response_url="https://fixture.atende.net/xhr/plugin/fixture",
        rendered_html=_read("plugin_portal_rendered.html"),
    )
    frame = propose_candidates(
        SERVICE_URL,
        page_html=_shell(),
        iframe_capture={
            "detail_url": SERVICE_URL + "/detalhar/1",
            "iframe_src": "https://fixture.atende.net/frame/publico",
            "response_url": "https://fixture.atende.net/frame/api/lista",
            "rendered_html": _read("plugin_portal_rendered.html"),
            "rows": _payload(),
        },
    )
    for proposal in (*page, *frame):
        assert proposal["disposition"] == "propose"
        assert proposal["confirmed"] is False
        assert "confirmado" not in json.dumps(proposal, ensure_ascii=False).casefold()
