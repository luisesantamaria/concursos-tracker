from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.fase2_municipios.v2.eval.f3_datatables_adapter import (
    DataTablesAdapterError,
    detect_datatables_server_side,
    propose_candidates,
)


FIXTURES = Path(__file__).parent / "fixtures" / "f3_datatables"
PAGE_URL = (
    "https://acessoinformacao.org.br/licitacoes/entidades/rs/"
    "fortalezadosvalos/documentos?tipo=concurso-publico"
)
DELEGATION_PROOF = "https://pmfv.rs.gov.br/documentos?tipo=concurso-publico"


def page_html() -> str:
    return (FIXTURES / "fortalezadosvalos_server_side.html").read_text(encoding="utf-8")


def raw_response() -> bytes:
    return (FIXTURES / "fortalezadosvalos_response_contract.json").read_bytes()


class RecordingFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, *, headers: dict[str, str]) -> bytes:
        self.calls.append((url, headers))
        return self.payload


def test_detects_server_side_datatables_from_observed_surface_fixture() -> None:
    detection = detect_datatables_server_side(page_html(), PAGE_URL)

    assert detection is not None
    assert detection.endpoint.endswith("/fortalezadosvalos/documentos")
    assert "server_side_true" in detection.signals
    assert detection.columns == ("tipo", "numero", "titulo", "data_inicio", "arquivo", "ano")


@pytest.mark.parametrize(
    "html",
    [
        "<html><table><tr><td>Concurso Público</td></tr></table></html>",
        "<script>$('#x').DataTable({serverSide: false, data: []});</script>",
        "<script src='jquery.dataTables.min.js'></script>",
    ],
)
def test_rejects_html_without_server_side_datatables(html: str) -> None:
    assert detect_datatables_server_side(html, PAGE_URL) is None


def test_reproduces_only_the_observed_public_query_and_header() -> None:
    fetcher = RecordingFetcher(raw_response())

    proposals = propose_candidates(page_html(), PAGE_URL, DELEGATION_PROOF, fetcher)

    assert len(fetcher.calls) == 1
    request_url, headers = fetcher.calls[0]
    assert headers == {"X-Requested-With": "XMLHttpRequest"}
    assert urlsplit(request_url).path.endswith("/fortalezadosvalos/documentos")
    assert parse_qs(urlsplit(request_url).query) == {
        "tipo": ["concurso-publico"],
        "draw": ["1"],
        "start": ["0"],
        "length": ["100"],
    }
    assert len(proposals) == 3


def test_missing_delegation_proof_refuses_before_fetching() -> None:
    fetcher = RecordingFetcher(raw_response())

    with pytest.raises(DataTablesAdapterError, match="delegation_proof is required"):
        propose_candidates(page_html(), PAGE_URL, None, fetcher)

    assert fetcher.calls == []


def test_normalizes_title_date_attachments_and_process_number_year() -> None:
    proposals = propose_candidates(
        page_html(), PAGE_URL, DELEGATION_PROOF, RecordingFetcher(raw_response())
    )

    first = proposals[0]
    assert first["title"] == "Edital nº 01/2023 - Concurso Público"
    assert first["date"] == "10/01/2023"
    assert first["process_number"] == "01"
    assert first["process_year"] == "2023"
    assert first["attachments"] == [
        {
            "title": "Edital de abertura",
            "url": "https://acessoinformacao.org.br/arquivos/edital-01-2023.pdf",
        }
    ]
    assert first["disposition"] == "propose"
    assert first["confirmed"] is False
    assert first["evidence"]["item_positive"] is True


def test_raw_json_hash_is_stable_for_the_same_input() -> None:
    raw = raw_response()
    first = propose_candidates(page_html(), PAGE_URL, DELEGATION_PROOF, RecordingFetcher(raw))
    second = propose_candidates(page_html(), PAGE_URL, DELEGATION_PROOF, RecordingFetcher(raw))

    expected = sha256(raw).hexdigest()
    assert first[0]["evidence"]["raw_response_sha256"] == expected
    assert second[0]["evidence"]["raw_response_sha256"] == expected
    assert first[0]["evidence"]["raw_response_json"].encode("utf-8") == raw


def test_negative_detection_does_not_call_fetcher() -> None:
    fetcher = RecordingFetcher(raw_response())

    proposals = propose_candidates(
        "<html><p>Lista estática</p></html>", PAGE_URL, DELEGATION_PROOF, fetcher
    )

    assert proposals == []
    assert fetcher.calls == []


def test_rejects_tipo_not_observed_in_page_url() -> None:
    fetcher = RecordingFetcher(raw_response())

    with pytest.raises(DataTablesAdapterError, match="observed 'tipo'"):
        propose_candidates(
            page_html(), PAGE_URL.split("?")[0], DELEGATION_PROOF, fetcher
        )

    assert fetcher.calls == []


def test_same_delegated_host_is_not_accepted_as_delegation_proof() -> None:
    with pytest.raises(DataTablesAdapterError, match="distinct official origin"):
        propose_candidates(page_html(), PAGE_URL, PAGE_URL, RecordingFetcher(raw_response()))
