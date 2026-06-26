# Instructions for Codex Agents

Read this file before editing the project.

## Project Orientation

- Work from the repository root.
- Treat `authority_first/` as the canonical implementation.
- Treat `laboratorio/`, old `scripts/`, `data/`, `output/`, and `logs/` as legacy/reference unless the user explicitly asks otherwise.
- The pilot scope is RS only. Do not expand to other states unless the user explicitly changes the scope.

## Core Rules

- Do not hardcode one-off URLs or portal-specific patterns just to pass a sample. Each hardcoded rule fixes one site and breaks the next. Generalize the behavior and validate with the golden set.
- Source authority order:
  1. Banca organizadora.
  2. Prefeitura/official organ site.
  3. Diario/FAMURS/publication portals.
  4. Radar portals only for discovery/audit.
- Radar portals such as Ache Concursos, PCI, QConcursos, Folha Dirigida, Estrategia and Gran are not final evidence.
- Prefer the stable **index/listing page** that aggregates all documents for a bucket (concursos or processos seletivos), not a single news article or individual edital detail page.
- For Fundatec, `pagina_editais.php` is usually the official document base; use `index_concursos.php` only to discover missing abertura PDF links when needed.
- For municipality pages, prefer the best category page, not the first URL that validates. When multiple siblings verify (e.g., "Processo Seletivo" vs "Processos Seletivos Simplificados"), use Gemini to pick the most complete one — do NOT add scoring constants.

## Architecture: No Numeric Scorers

The previous scorer approach (candidate_page_quality, bucket_dominance_score, process_family_score, etc. with ~50 magic numbers) was deliberately abandoned. It created an unmanageable system where each fix broke something else.

The replacement:
1. **Deterministic verification** (content signals) decides whether a page is valid for a bucket.
2. **Gemini ai_pick_best** decides which valid candidate is the best index page — by understanding content, not by summing points.
3. **Discrete decisions** (indice_oficial, revisar, nao_encontrado) replace numeric scores.

If you feel tempted to add a `+150` or `-220` constant to a scorer, STOP. Either:
- Let the AI verifier/picker handle it (generalizes to unseen portals).
- Accept that the case needs `revisar` (honest is better than wrong).
- Add it to the golden set so it becomes a measurable regression test.

## AI Usage

- Prefer deterministic crawling first, then Gemini as verifier/fallback.
- Use `gemini-2.5-flash` by default. Avoid Flash Lite unless the user asks for cheaper exploratory runs.
- Gemini has two distinct roles in the pipeline:
  1. **Discoverer** (Tier 2): Grounded search to find URLs the free tier missed. One call, only when needed.
  2. **Selector** (Tier 3): Pick the best candidate among verified pages. No grounding needed, cheap.
- AI can validate and investigate, but official page content remains the source of truth.
- Record why a row is `revisar` or `nao_encontrada`; empty comments make review painful.

## Playwright Usage

Playwright is the Tier 4 last resort. Rules:
- Never use it by default. Only when Tiers 0-3 failed AND the page shows JS-rendering signals or menu buttons jump to unpredictable destinations.
- **Directed, not blind**: Navigate menus by their text (Publicacoes, Concursos, etc.), do not crawl the entire site.
- Reuse ONE browser instance for the entire run. Do not launch/close Chromium per URL.
- Detect JS-rendering need before launching: look for `<div id="root">`, `<div id="app">`, empty `<body>` with many `<script>` tags.

## Golden Set

The golden set (`authority_first/data/golden_set_v1.csv`) is the independent ground truth. It was built by hand, not by the pipeline.

- Run the evaluator after ANY change to verification, selection, or cascade logic.
- The metric to optimize is **precision** (zero false positives), not coverage.
- Coverage gaps are acceptable and can be filled later or manually.
- The golden set includes ~20% of municipalities marked as requiring human review — that is the expected ceiling for full automation.

## Development

- Use Python 3.12.
- Use `requirements.txt` for dependencies.
- Use `rg` for searches.
- Do not commit generated outputs, logs, caches, credentials, model weights, or local assistant settings.
- If touching crawlers, run a small sample before a broad run.
- If touching validators or selection logic, run golden-set evaluation.
- Cache grounding results to disk by municipality for reproducibility and cost savings.
- Use real browser User-Agent headers in requests sessions to avoid 406 anti-bot blocks.

## Useful Commands

```bash
python authority_first/scripts/crawlers/crawl_bancas_base_rs.py --help
python authority_first/scripts/crawlers/crawl_municipios_resources_rs.py --help
python authority_first/scripts/crawlers/grounded_deepsearch_municipios_a.py --help
python authority_first/scripts/review/ai_repair_bancas_rs.py --help
python authority_first/scripts/eval/medir_golden_set.py --help
```

## Final Checklist

- Confirm no secrets are staged.
- Confirm generated outputs are ignored.
- Run relevant `--help` or small-run smoke checks.
- If validators or selection logic changed, run golden-set evaluation.
- If a new portal type was discovered, add a representative municipality to the golden set.
- Update docs when the pipeline behavior changes.
