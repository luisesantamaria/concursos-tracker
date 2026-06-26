# Claude Code Instructions

This project is Concursos Tracker, an authority-first crawler and validation pipeline for public contests and simplified selection processes in Rio Grande do Sul.

## Context

The project began as an Ache Concursos radar prototype and evolved into a source-authority model. The important principle is:

- Banca pages are authoritative for the active contest lifecycle.
- Prefeitura and Diario/FAMURS pages are authoritative for municipal-only processes and post-result administrative events.
- Radar portals are useful to discover candidates and audit coverage, but they are not final proof.

## Where to Work

- Scripts: `scripts/fase1_bancas/` (banca crawlers), `scripts/fase2_municipios/` (municipality cascade), `scripts/eval/` (golden set evaluator), `scripts/shared/` (RS scope library).
- Data and config: `authority_first/data/` (golden set, registry CSVs).
- Main plans/docs: root docs plus `authority_first/docs/`.
- Legacy experiments: `laboratorio/`.

Do not restart from scratch unless the user explicitly asks. Preserve what was learned in the lab.

## Commands

Install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Smoke checks:

```bash
python scripts/fase1_bancas/crawl_bancas_base_rs.py --help
python scripts/fase1_bancas/ai_repair_bancas_rs.py --help
python scripts/fase2_municipios/cascade_municipios_rs.py --help
```

Golden-set evaluation:

```bash
python scripts/eval/medir_golden_set.py --golden authority_first/data/golden_set_v1.csv --pipeline <output.csv> --detalle
```

## Current Phase: Municipality Resource Discovery

The current focus is **finding the stable index/listing page** for concursos and processos seletivos in each RS municipality. This phase does NOT extract individual editals, PDFs, or event details — that is a later phase.

### What counts as a valid URL in this phase

- Page index / category page / listing page / portal showing all concursos or PSS.
- Page with filters, cards, or list of multiple events.
- Parent page from which you navigate into individual editals.

### What we reject in this phase

- PDF directo.
- Individual edital page (`/detalhe/452/...`).
- Single news article about one concurso.
- Specific annex, cronograma, or retificacao.
- Licitacao, pregao, chamamento publico.
- Cultural contest (concurso de soberanas/rainhas).

If only a detail page is found but not the index, do not accept it as correct. Mark it `revisar` with a note explaining that the index page is missing.

## Pipeline Architecture: 5-Tier Cascade

The municipality resource discovery pipeline uses a cascade that spends expensive tools only when cheap ones fail. The tiers are:

1. **Tier 0 — Site oficial**: Find or confirm the prefeitura's base domain.
2. **Tier 1 — Free link discovery**: Follow HTML menus, anchors, sitemap, portal da transparencia. Pure requests, no AI.
3. **Tier 2 — Grounded search**: If something is still missing, use Gemini with Google Search grounding. One call per municipality, only when needed.
4. **Tier 3 — Gemini verifier/selector**: Receives candidate URLs and makes discrete classification decisions (not scores). Picks the best index page among valid candidates.
5. **Tier 4 — Navigation agent (Playwright)**: Last resort. Opens the site in a headless browser and navigates menus like a human — directed by menu text, not blind crawling. Only for municipalities where buttons jump to unpredictable destinations (IP portals, JS-rendered pages, external transparency systems).

### Critical rule: no numeric scorers

Do NOT use numeric scoring systems (score=85, score>=110, candidate_page_quality, bucket_dominance_score, etc.) to choose between candidate URLs. These interact unpredictably with 50+ magic constants and break when a new portal type appears.

Instead, use **discrete decisions**:
- `indice_oficial` — stable listing page found.
- `indice_oficial_combinado` — single page serves both concursos and PSS.
- `portal_externo_oficial` — external portal (IP, atende.net) reached from official menu.
- `detalle_individual_rechazado` — only found a detail page, not the index.
- `licitacao_rechazada` — page is licitacoes, not concursos.
- `concurso_cultural_rechazado` — soberanas/rainhas, not public selection.
- `nao_encontrado` — genuinely not found.
- `revisar` — ambiguous, needs human eyes.

When multiple candidates verify as valid, use Gemini to **pick the best one by content** (ai_pick_best), not a point system. The LLM understands "Processos Seletivos Simplificados is more complete than Processo Seletivo" without needing a hardcoded rule.

### Precision over coverage

- Zero false positives matters more than high coverage.
- A 3/5 with all correct is better than 5/5 with one wrong URL.
- ~20% of municipalities may require human review — that is acceptable.
- A pipeline that says "I don't know, review this" is superior to one that invents a URL.

## Pipeline Rules

- Scope remains RS only.
- Do not accept federal/national contests only because tests happen in RS.
- For statewide contests, set municipality as `Estatal`.
- Keep `concurso_publico` and `processo_seletivo` separate.
- The desired base fields are: `tipo`, `orgao`, `municipio`, `uf`, `numero`, `banca`, `edital_pagina`, `edital_pdf`.
- A row should be `listo` only when page, PDF, number, year and identity match.
- If a row is `revisar`, explain why.
- Do not hardcode one-off URLs or portal-specific patterns (multi24, secao=dinamico, specific IP addresses) as scoring rules. Each rule fixes one case and breaks the next portal. If a pattern is provider-specific, let the AI verifier handle it or accept `revisar`.

## Golden Set

The golden set (`authority_first/data/golden_set_v1.csv`) contains 24 municipalities verified by hand with the correct URLs. It is the **independent ground truth** — never generated by the pipeline.

Key examples encoded in the golden set:
- **Arambare**: Do not accept "Processo Seletivo" if "Processos Seletivos Simplificados" exists and is more complete.
- **Ararica**: Accept external portal/IP if reached from official menu buttons.
- **Agua Santa**: Prefer ano=0 (all years) over year-filtered views.
- **Alto Feliz**: A page mentioning "Concurso Soberanas" (cultural) is NOT a public selection signal.
- **Acegua**: Accept atende.net as official delegated portal.
- **Porto Alegre, Pelotas, Gravatai, Sao Leopoldo**: Some municipalities genuinely require human review.

Run the golden set evaluator after ANY change to verification, selection, or cascade logic:

```bash
python authority_first/scripts/eval/medir_golden_set.py --golden authority_first/data/golden_set_v1.csv --pipeline <output.csv> --detalle
```

## Known Tricky Sources

- Fundatec: abertura PDFs can appear from `index_concursos.php`, while the document base is `pagina_editais.php`.
- Legalle has at least two portals: `portal.editais.legalleconcursos.com.br` and `portal.institutolegalle.org.br`.
- La Salle can throttle or block if crawled too aggressively.
- FAURGS and some municipal sites use buttons/download handlers that may require deeper link extraction.
- Municipality menus can hide process pages under `Editais`, `Publicacoes`, `Transparencia`, `Concursos`, or external transparency systems.
- Portal delegado (multi24, oxy.elotech, govbr.cloud): buttons on the official site may jump to an IP address or external domain. The destination is only discoverable by following the click.
- Some municipalities list PSS under multiple sibling menu items (e.g., "Processo Seletivo" vs "Processos Seletivos Simplificados"). The more complete/updated one is correct.
- ~21% of municipalities require human review due to unusual portal architectures (combobox selection, embedded portals, base64 hashes in URLs, scattered annexes).

## Safety

Never commit `.env`, local API keys, generated outputs, logs, model files, Cloud Run/RunPod secrets, or `.claude/settings.local.json`.
