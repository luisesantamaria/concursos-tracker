from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "fase2_municipios"))

import cascade_municipios as C  # noqa: E402


AUDIT_PATH = ROOT / "data" / "fase2" / "municipios_rs_local_auditoria.csv"
RENDER_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "render"

CONCURSOS_TO_PROCESSOS = [
    "Ametista Do Sul",
    "Boa Vista Das Missões",
    "Capão Bonito Do Sul",
    "Gentil",
    "Ijuí",
    "Paraí",
    "Porto Mauá",
    "Redentora",
    "Rio Pardo",
    "São Pedro Das Missões",
    "Sete De Setembro",
    "Viadutos",
]

PROCESSOS_TO_CONCURSOS = [
    "David Canabarro",
    "Dezesseis De Novembro",
    "Giruá",
    "Independência",
]

CULTURAL_REJEITADO = [
    "Colinas",
    "Garruchos",
    "São Francisco De Paula",
]


def _raw_render_fixtures() -> dict[str, dict]:
    fixtures = {}
    for path in RENDER_FIXTURE_DIR.glob("*.json"):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        url = fixture.get("url") or ""
        if url:
            fixtures[C._normalized_candidate_url(url)] = fixture
    return fixtures


RAW_RENDER_FIXTURES = _raw_render_fixtures()


def _candidate(url: str, bucket_hint: str, *, title: str = "") -> C.Candidate:
    raw = RAW_RENDER_FIXTURES.get(C._normalized_candidate_url(url), {})
    links = [
        (anchor.get("href", ""), anchor.get("text", ""))
        for anchor in (raw.get("anchors") or [])
    ]
    page = C.Page(
        url=url,
        status=200,
        title=raw.get("title") or title,
        text=raw.get("text") or ("conteudo salvo " * 40),
        links=links,
    )
    return C.Candidate(
        url=url,
        source="offline_fixture",
        page=page,
        content_preview=page.text,
        bucket_hint=bucket_hint,
    )


def _route_one(url: str, bucket_hint: str, forma: str, tipo: str) -> dict:
    return C._route_classified_candidates(
        [_candidate(url, bucket_hint)],
        [{"id": 0, "forma": forma, "tipo": tipo, "razao": "auditoria fechada"}],
    )


def _audit_rows() -> dict[tuple[str, str], dict[str, str]]:
    with AUDIT_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return {
            (row["municipio"], row["bucket"]): row
            for row in csv.DictReader(handle)
        }


def test_run497_offline_replay_uses_closed_audit_verdicts():
    """Replay stored candidates; forma/tipo are the closed audit ground truth."""
    rows = _audit_rows()
    raw_matches = {
        municipio
        for (municipio, _bucket), row in rows.items()
        if C._normalized_candidate_url(row["url"]) in RAW_RENDER_FIXTURES
    }
    # Of the 20 closed-audit URLs, only David Canabarro has an exact saved
    # rendered-page fixture. The remaining replay inputs are URL+audit verdict.
    assert "David Canabarro" in raw_matches

    for municipio in CONCURSOS_TO_PROCESSOS:
        fixture = rows[(municipio, "concursos")]
        assert fixture["severidad"] == "hard"
        result = _route_one(fixture["url"], "concursos", "indice", "pss")
        assert result["url_concursos"] == "", municipio
        assert result["decision_concursos"] == "nao_encontrado", municipio
        assert result["url_processos_seletivos"] == fixture["url"], municipio
        assert result["decision_processos"] == "indice_oficial", municipio

    for municipio in PROCESSOS_TO_CONCURSOS:
        fixture = rows[(municipio, "processos")]
        assert fixture["severidad"] == "hard"
        result = _route_one(fixture["url"], "processos", "indice", "concurso")
        assert result["url_processos_seletivos"] == "", municipio
        assert result["decision_processos"] == "nao_encontrado", municipio
        assert result["url_concursos"] == fixture["url"], municipio
        assert result["decision_concursos"] == "indice_oficial", municipio

    for municipio in CULTURAL_REJEITADO:
        fixture = rows[(municipio, "concursos")]
        assert fixture["severidad"] == "hard"
        result = _route_one(fixture["url"], "concursos", "cultural", "incierto")
        assert result["url_concursos"] == "", municipio
        assert result["url_processos_seletivos"] == "", municipio
        assert result["decision_concursos"] == "concurso_cultural_rechazado", municipio

    tapes = rows[("Tapes", "processos")]
    assert tapes["severidad"] == "hard"
    result = _route_one(tapes["url"], "processos", "noticia", "pss")
    assert result["url_concursos"] == ""
    assert result["url_processos_seletivos"] == ""
    assert result["decision_processos"] == "detalle_individual_rechazado"


def test_reassigned_candidate_competes_with_occupied_destination():
    reassigned = _candidate("https://example.test/indice-a", "concursos")
    incumbent = _candidate("https://example.test/indice-b", "processos")
    classifications = [
        {"id": 0, "forma": "indice", "tipo": "pss"},
        {"id": 1, "forma": "indice", "tipo": "pss"},
    ]
    without_pick = C._route_classified_candidates(
        [reassigned, incumbent],
        classifications,
    )
    assert without_pick["url_processos_seletivos"] == ""
    assert without_pick["decision_processos"] == "revisar"

    result = C._route_classified_candidates(
        [reassigned, incumbent],
        classifications,
        {"processos": 1},
    )
    assert result["url_concursos"] == ""
    assert result["url_processos_seletivos"] == incumbent.url


def test_normalized_duplicate_collapses_before_selector():
    first = _candidate("http://www.example.test/lista/?b=2&a=1", "concursos")
    duplicate = _candidate("https://example.test/lista?a=1&b=2", "processos")
    result = C._route_classified_candidates(
        [first, duplicate],
        [
            {"id": 0, "forma": "indice", "tipo": "pss"},
            {"id": 1, "forma": "indice", "tipo": "pss"},
        ],
    )
    assert result["url_processos_seletivos"] == first.url
    assert result["decision_processos"] == "indice_oficial"


def test_mixed_index_uses_same_url_for_both_buckets():
    combined = _candidate("https://example.test/editais", "ambos")
    result = C._route_classified_candidates(
        [combined],
        [{"id": 0, "forma": "indice", "tipo": "mixto"}],
    )
    assert result["url_concursos"] == combined.url
    assert result["url_processos_seletivos"] == combined.url
    assert result["decision_concursos"] == "indice_oficial_combinado"
    assert result["decision_processos"] == "indice_oficial_combinado"


def test_uncertain_form_reviews_origin_without_opposite_reassignment():
    result = _route_one(
        "https://example.test/candidato", "concursos", "incierto", "pss",
    )
    assert result["url_concursos"] == ""
    assert result["url_processos_seletivos"] == ""
    assert result["decision_concursos"] == "revisar"
    assert result["decision_processos"] == "nao_encontrado"


def test_uncertain_type_reviews_origin_without_assignment():
    result = _route_one(
        "https://example.test/candidato", "processos", "indice", "incierto",
    )
    assert result["url_concursos"] == ""
    assert result["url_processos_seletivos"] == ""
    assert result["decision_processos"] == "revisar"


def test_originless_uncertainty_reviews_both_buckets():
    candidate = _candidate("https://example.test/opaque", "")
    for forma, tipo in [("incierto", "pss"), ("indice", "incierto")]:
        result = C._route_classified_candidates(
            [candidate], [{"id": 0, "forma": forma, "tipo": tipo}],
        )
        assert result["url_concursos"] == ""
        assert result["url_processos_seletivos"] == ""
        assert result["decision_concursos"] == "revisar"
        assert result["decision_processos"] == "revisar"


def test_cultural_precedes_type_and_is_never_reassigned():
    result = _route_one(
        "https://example.test/soberanas", "concursos", "cultural", "pss",
    )
    assert result["url_concursos"] == ""
    assert result["url_processos_seletivos"] == ""
    assert result["decision_concursos"] == "concurso_cultural_rechazado"


def test_detail_and_news_variants_never_enter_index_pools():
    news = _candidate("https://example.test/noticia", "processos")
    news.page.links.append(("https://example.test/edital.pdf", "Baixar edital"))
    individual = _candidate("https://example.test/edital/42", "processos")
    spa = _candidate("https://example.test/app", "processos")
    spa.page.is_spa = True
    historical = _candidate(
        "https://example.test/historico", "processos", title="Histórico",
    )
    historical.page.text = "Processo seletivo 001/2020"
    variants = [
        ("noticia_com_pdf", news, "noticia"),
        ("edital_individual", individual, "detalle"),
        ("spa_processo_unico", spa, "detalle"),
        ("historico_registro_unico", historical, "detalle"),
    ]
    for label, candidate, forma in variants:
        result = C._route_classified_candidates(
            [candidate], [{"id": 0, "forma": forma, "tipo": "pss"}],
        )
        assert result["url_concursos"] == "", label
        assert result["url_processos_seletivos"] == "", label
        assert result["decision_processos"] == "detalle_individual_rechazado", label


def test_tier3_single_call_parses_dimensions_then_applies_selector():
    candidates = [
        _candidate("https://example.test/reassigned", "concursos"),
        _candidate("https://example.test/incumbent", "processos"),
    ]
    response = {
        "candidates": [{
            "content": {"parts": [{"text": (
                '{"classificacoes":['
                '{"id":0,"forma":"indice","tipo":"pss"},'
                '{"id":1,"forma":"indice","tipo":"pss"}],'
                '"melhor_id_concursos":null,"melhor_id_processos":1,'
                '"razao":"incumbente mais completo"}'
            )}]}
        }]
    }
    with patch.object(C, "gemini_post", return_value=response) as post:
        result = C.tier3_classify_and_pick(
            object(), "gemini-2.5-flash", "Fixture", candidates,
        )
    post.assert_called_once()
    assert result["url_concursos"] == ""
    assert result["url_processos_seletivos"] == candidates[1].url
    assert [item["forma"] for item in result["classificacoes"]] == ["indice", "indice"]
    assert [item["tipo"] for item in result["classificacoes"]] == ["pss", "pss"]


def test_tier3_preserves_combined_fill_render_evidence_for_spa():
    combined = _candidate("https://example.test/app", "ambos")
    combined.page.is_spa = True
    rendered = "RENDERED_MIXED_INDEX " + ("Concurso Público Processo Seletivo Edital 001/2025 " * 20)
    response = {
        "candidates": [{
            "content": {"parts": [{"text": (
                '{"classificacoes":['
                '{"id":0,"forma":"indice","tipo":"mixto"}],'
                '"melhor_id_concursos":0,"melhor_id_processos":0,'
                '"razao":"indice misto renderizado"}'
            )}]}
        }]
    }

    def gemini_response(_session, _model, payload, timeout):
        assert timeout == 60
        prompt_text = payload["contents"][0]["parts"][0]["text"]
        assert "RENDERED_MIXED_INDEX" in prompt_text
        return response

    with patch.object(C, "_render_text", return_value=rendered) as render:
        with patch.object(C, "gemini_post", side_effect=gemini_response):
            result = C.tier3_classify_and_pick(
                object(), "gemini-2.5-flash", "Fixture", [combined],
            )
    render.assert_called_once_with(combined.url)
    assert result["url_concursos"] == combined.url
    assert result["url_processos_seletivos"] == combined.url
    assert result["decision_concursos"] == "indice_oficial_combinado"
    assert result["decision_processos"] == "indice_oficial_combinado"


def test_incomplete_later_tier_does_not_erase_complete_selection():
    home = C.Page(
        url="https://municipio.test", status=200,
        text="Prefeitura Municipal " * 40,
    )
    concurso = _candidate("https://municipio.test/concursos", "concursos")
    probe = _candidate("https://municipio.test/opaque", "processos")
    complete = {
        "url_concursos": concurso.url,
        "url_processos_seletivos": "",
        "decision_concursos": "indice_oficial",
        "decision_processos": "nao_encontrado",
        "classificacoes": [{"id": 0, "forma": "indice", "tipo": "concurso"}],
        "classification_complete": True,
        "razao": "concurso confirmado",
    }
    incomplete = C._empty_tier3_result()

    with patch.object(C, "tier0_find_site", return_value=home):
        with patch.object(C, "tier1_collect_candidates", return_value=[concurso]):
            with patch.object(C, "gemini_api_key", return_value="offline-key"):
                with patch.object(
                    C, "tier3_classify_and_pick", side_effect=[complete, incomplete],
                ):
                    with patch.object(C, "_probe_known_index_paths", return_value=[probe]):
                        with patch.object(C, "tier2_grounded_search", return_value=[]):
                            with patch.object(C, "tier2_directed_bucket_search", return_value=[]):
                                result = C.process_municipio(
                                    object(), "Fixture", "gemini-2.5-flash",
                                    use_playwright=False,
                                )

    assert result.url_concursos == concurso.url
    assert result.tier_concursos == "t1"
    assert result.confianza_concursos == "confirmado"
    assert "tier3 incomplete; previous selection preserved" in result.razao
