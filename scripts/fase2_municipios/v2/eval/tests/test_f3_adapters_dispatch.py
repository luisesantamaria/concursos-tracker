"""Integration tests for F3 adapter dispatch (stdlib unittest, no sockets)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.fase2_municipios.v2.eval.f3_adapters_dispatch import dispatch_f3_adapter
from scripts.fase2_municipios.v2.eval.grounded_rescue import (
    Target,
    run_micro_acquisitions,
)
from scripts.fase2_municipios.v2.eval.platform_probe_runner import FetchResult


class StaticFetcher:
    def __init__(self, html: str) -> None:
        self.html = html
        self.urls: list[str] = []

    def get(self, url: str, timeout: int) -> FetchResult:
        self.urls.append(url)
        return FetchResult(status_code=200, html=self.html, final_url=url)


def _multi_result() -> SimpleNamespace:
    item = SimpleNamespace(title="Concurso Publico 01/2026", url="https://docs.test/1.pdf")
    candidate = SimpleNamespace(
        node_url="https://sistemas.exemplo.rs.gov.br/multi24/concursos/2026",
        label="Concursos 2026",
        provenance=("href:/multi24/concursos/2026",),
        items=(item,),
    )
    return SimpleNamespace(
        disposition="candidata",
        reason="candidate_nodes_with_item_evidence",
        candidates=(candidate,),
        raw_sha256_by_url=((candidate.node_url, "abc123"),),
        authority_evidence=("official_source_sha256:def456",),
        identity_evidence=("municipio:Exemplo",),
        platform_evidence=("multi24",),
    )


class F3AdapterDispatchIntegrationTests(unittest.TestCase):
    def test_atende_pss_candidate_is_rejected_for_cp_with_bucket_mismatch(self) -> None:
        proposal = {
            "url_candidata": "https://exemplo.atende.net/cidadao/pagina/editais",
            "title": "Processo Seletivo Simplificado 01/2026",
            "evidence": {
                "row_text": "PSS 01/2026 - Edital de abertura",
                "raw_response_sha256": "pss-hash",
            },
        }
        with patch(
            "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
            "f3_atende_adapter.propose_candidates",
            return_value=[proposal],
        ):
            result = dispatch_f3_adapter(
                url="https://exemplo.atende.net/cidadao/pagina/editais",
                page_html="<html>shell</html>",
                municipio="exemplo",
                bucket="concurso_publico",
            )

        self.assertEqual([], result["candidates"])
        self.assertEqual("bucket_mismatch", result["refusal_reason"])
        provenance = result["rejected_candidates"][0]["provenance"]
        self.assertEqual("bucket_mismatch", provenance["reason"])
        self.assertEqual("processo_seletivo", provenance["classified_bucket"])

    def test_atende_cp_candidate_passes_for_cp(self) -> None:
        proposal = {
            "url_candidata": "https://exemplo.atende.net/cidadao/pagina/concursos",
            "title": "Concurso Público 01/2026",
            "evidence": {
                "row_text": "Concurso Público 01/2026 - Edital de abertura",
                "raw_response_sha256": "cp-hash",
            },
        }
        with patch(
            "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
            "f3_atende_adapter.propose_candidates",
            return_value=[proposal],
        ):
            result = dispatch_f3_adapter(
                url="https://exemplo.atende.net/cidadao/pagina/concursos",
                page_html="<html>shell</html>",
                municipio="exemplo",
                bucket="concurso_publico",
            )

        self.assertEqual(1, len(result["candidates"]))
        self.assertEqual([], result["rejected_candidates"])
        self.assertEqual("", result["refusal_reason"])

    def test_datatables_ambiguous_candidate_is_rejected(self) -> None:
        detection = SimpleNamespace(endpoint="https://delegado.test/ajax", columns=(), signals=())
        proposal = {
            "source_url": "https://delegado.test/lista",
            "title": "Edital 01/2026",
            "evidence": {
                "quote": "Edital 01/2026 - inscrições abertas",
                "raw_response_sha256": "ambiguous-hash",
            },
        }
        with (
            patch(
                "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
                "f3_datatables_adapter.detect_datatables_server_side",
                return_value=detection,
            ),
            patch(
                "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
                "f3_datatables_adapter.propose_candidates",
                return_value=[proposal],
            ),
        ):
            result = dispatch_f3_adapter(
                url="https://delegado.test/lista?tipo=Concurso",
                page_html="<script>DataTable</script>",
                municipio="exemplo",
                bucket="concurso_publico",
                context={
                    "delegation_proof": "https://exemplo.rs.gov.br/concursos",
                    "datatables_fetcher": lambda *_args, **_kwargs: {},
                },
            )

        self.assertEqual([], result["candidates"])
        provenance = result["rejected_candidates"][0]["provenance"]
        self.assertEqual("bucket_mismatch", provenance["reason"])
        self.assertEqual("ambiguous", provenance["classified_bucket"])

    def test_multi24_contract_bucket_is_not_filtered_twice(self) -> None:
        item = SimpleNamespace(title="Edital 01/2026", url="https://docs.test/1.pdf")
        candidate = SimpleNamespace(
            node_url="https://sistemas.exemplo.rs.gov.br/multi24/editais/2026",
            label="Editais 2026",
            provenance=("href:/multi24/editais/2026",),
            items=(item,),
        )
        contract_bucketed = SimpleNamespace(
            disposition="candidata",
            reason="candidate_nodes_with_item_evidence",
            candidates=(candidate,),
            raw_sha256_by_url=((candidate.node_url, "abc123"),),
            authority_evidence=("official_source_sha256:def456",),
            identity_evidence=("municipio:Exemplo",),
            platform_evidence=("multi24",),
        )
        with patch(
            "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
            "f3_multi24_adapter.analyze_multi24",
            return_value=contract_bucketed,
        ):
            result = dispatch_f3_adapter(
                url="https://sistemas.exemplo.rs.gov.br/multi24/sistemas/transparencia/index",
                page_html="<html>Multi24</html>",
                municipio="exemplo",
                bucket="concurso_publico",
                current_year=2026,
                context={"multi24_authority": object(), "multi24_linked_pages": {}},
            )

        self.assertEqual(1, len(result["candidates"]))
        self.assertNotIn("rejected_candidates", result)

    def test_dispatches_multi24_atende_and_datatables_by_platform(self) -> None:
        with patch(
            "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
            "f3_multi24_adapter.analyze_multi24",
            return_value=_multi_result(),
        ) as multi:
            result = dispatch_f3_adapter(
                url="https://sistemas.exemplo.rs.gov.br/multi24/sistemas/transparencia/index",
                page_html="<html>Multi24</html>",
                municipio="exemplo",
                bucket="concurso_publico",
                current_year=2026,
                context={"multi24_authority": object(), "multi24_linked_pages": {}},
            )
        self.assertTrue(multi.called)
        self.assertEqual("multi24", result["platform"])

        atende_proposal = {
            "url_candidata": "https://exemplo.atende.net/cidadao/pagina/concursos",
            "disposition": "propose",
            "confirmed": True,
            "title": "Concurso Publico 01/2026",
            "evidence": {"raw_response_sha256": "atende-hash"},
        }
        with patch(
            "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
            "f3_atende_adapter.propose_candidates",
            return_value=[atende_proposal],
        ) as atende:
            result = dispatch_f3_adapter(
                url="https://exemplo.atende.net/cidadao/pagina/concursos",
                page_html="<html>shell</html>",
                municipio="exemplo",
                bucket="concurso_publico",
            )
        self.assertTrue(atende.called)
        self.assertEqual("atende", result["platform"])
        self.assertFalse(result["candidates"][0]["confirmed"])

        detection = SimpleNamespace(endpoint="https://delegado.test/ajax", columns=(), signals=())
        with (
            patch(
                "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
                "f3_datatables_adapter.detect_datatables_server_side",
                return_value=detection,
            ),
            patch(
                "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
                "f3_datatables_adapter.propose_candidates",
                return_value=[],
            ) as datatables,
        ):
            result = dispatch_f3_adapter(
                url="https://delegado.test/lista?tipo=Concurso",
                page_html="<script>DataTable</script>",
                municipio="exemplo",
                bucket="concurso_publico",
                context={
                    "delegation_proof": "https://exemplo.rs.gov.br/concursos",
                    "datatables_fetcher": lambda *_args, **_kwargs: {},
                },
            )
        self.assertTrue(datatables.called)
        self.assertEqual("datatables", result["platform"])

    def test_adapter_candidate_enters_runner_flow_with_provenance_and_never_confirms(self) -> None:
        url = "https://exemplo.atende.net/cidadao/pagina/concursos"
        target = Target("exemplo", "concurso_publico", url, "render_incierto")
        summary = {
            "unidades": {
                "exemplo/concurso_publico": {
                    "candidatas": [],
                    "micro_pendientes": [{
                        "url": url,
                        "query": "q",
                        "snippet": "s",
                        "host_oficial_check": "delegated",
                        "fuente": "grounding",
                        "redirector_original": "",
                    }],
                }
            },
            "global": {"candidatas": 0},
            "policy": {},
        }

        def dispatcher(**kwargs):
            return {
                "platform": "atende",
                "adapter": "f3_atende_adapter",
                "source_url": kwargs["url"],
                "candidates": [{
                    "url_candidata": kwargs["url"],
                    "disposition": "propose",
                    "confirmed": False,
                    "item_markers": 1,
                    "provenance": {
                        "adapter": "f3_atende_adapter",
                        "source_url": kwargs["url"],
                        "snapshot_sha256": "fixture-hash",
                    },
                }],
            }

        with tempfile.TemporaryDirectory() as directory:
            rows = run_micro_acquisitions(
                [target],
                [],
                summary,
                output_dir=Path(directory),
                fetcher=StaticFetcher("<html>shell</html>"),
                timestamp_run="2026-07-13T12:00:00+00:00",
                renderer=lambda _url: self.fail("generic render must follow only adapter refusal"),
                adapter_dispatcher=dispatcher,
            )
        self.assertEqual(1, len(rows))
        self.assertFalse(rows[0].confirmed)
        self.assertEqual("propose", rows[0].disposition)
        self.assertEqual("fixture-hash", rows[0].provenance["snapshot_sha256"])
        self.assertFalse(summary["unidades"]["exemplo/concurso_publico"].get("confirmacion", False))

    def test_refusal_without_delegation_or_real_href_continues_generic_flow(self) -> None:
        datatables_html = """
        <script>$('#lista').DataTable({serverSide: true, ajax: '/ajax'});</script>
        """
        refused = dispatch_f3_adapter(
            url="https://delegado.test/lista?tipo=Concurso",
            page_html=datatables_html,
            municipio="exemplo",
            bucket="concurso_publico",
        )
        self.assertEqual([], refused["candidates"])
        self.assertIn("delegation_proof", refused["refusal_reason"])

        url = "https://sistemas.exemplo.rs.gov.br/multi24/sistemas/transparencia/index"
        no_href = SimpleNamespace(
            disposition="revisar",
            reason="official_navigation_link_not_proven",
            candidates=(),
            raw_sha256_by_url=((url, "entry-hash"),),
        )
        with patch(
            "scripts.fase2_municipios.v2.eval.f3_adapters_dispatch."
            "f3_multi24_adapter.analyze_multi24",
            return_value=no_href,
        ):
            refused_multi = dispatch_f3_adapter(
                url=url,
                page_html="<html>Multi24 sem href oficial</html>",
                municipio="exemplo",
                bucket="concurso_publico",
                current_year=2026,
                context={"multi24_authority": object(), "multi24_linked_pages": {}},
            )
        self.assertEqual([], refused_multi["candidates"])
        self.assertEqual("official_navigation_link_not_proven", refused_multi["refusal_reason"])

        target = Target("exemplo", "concurso_publico", url, "render_incierto")
        summary = {
            "unidades": {"exemplo/concurso_publico": {
                "candidatas": [],
                "micro_pendientes": [{
                    "url": url, "query": "q", "snippet": "s",
                    "host_oficial_check": "delegated", "fuente": "grounding",
                    "redirector_original": "",
                }],
            }},
            "global": {"candidatas": 0},
            "policy": {},
        }
        render_calls: list[str] = []

        def renderer(render_url: str) -> SimpleNamespace:
            render_calls.append(render_url)
            return SimpleNamespace(
                final_url=render_url,
                text="Prefeitura de Exemplo\nConcurso Publico Edital 01/2026",
                status=200,
            )

        with tempfile.TemporaryDirectory() as directory:
            rows = run_micro_acquisitions(
                [target], [], summary,
                output_dir=Path(directory),
                fetcher=StaticFetcher("<html>Multi24 sem href oficial</html>"),
                timestamp_run="2026-07-13T12:00:00+00:00",
                renderer=renderer,
            )
        self.assertEqual([url], render_calls)
        self.assertEqual(1, len(rows))
        self.assertEqual("grounding", rows[0].fuente)
        self.assertFalse(rows[0].confirmed)


if __name__ == "__main__":
    unittest.main()
