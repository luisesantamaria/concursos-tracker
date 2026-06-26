# Concursos Tracker

Concursos Tracker is a data engine for discovering and validating public contest and simplified selection process sources in Brazil. The current pilot scope is **Rio Grande do Sul (RS)**.

The end goal is not a scraper — it is the **data engine for a matching app** where users register with their profile (escolaridade, profession, city + travel radius, minimum salary) and the app shows eligible concursos/PSS and sends lifecycle alerts.

## Authority-First Model

The project uses an authority-first sourcing model:

1. **Banca organizadora** when a banca exists.
2. **Prefeitura/official organ page** when there is no banca or for municipal follow-up.
3. **Diario/FAMURS/publication portals** for homologation, convocacao, nomeacao and long-tail administrative events.
4. **Radar portals** (Ache Concursos, PCI, QConcursos) only as discovery/audit, not as final authority.

## Current Phase: Municipality Resource Discovery

The current focus is discovering the **stable index/listing page** for concursos and processos seletivos in each RS municipality. This phase does NOT extract individual editals or PDFs.

### 5-Tier Cascade Architecture

```
Tier 0 — Site oficial:     Find/confirm the prefeitura's base domain.
Tier 1 — Free links:       Follow HTML menus, anchors, sitemap, transparencia.
Tier 2 — Grounded search:  Gemini + Google (only if Tier 1 incomplete).
Tier 3 — AI selector:      Gemini picks best index page among valid candidates.
Tier 4 — Navigation agent: Playwright navigates menus as last resort.
```

Key principles:
- **Content over slug**: a page at `/documentos` listing PSS is valid; an empty `/processos-seletivos` is not.
- **No numeric scorers**: discrete decisions + AI selection replace point systems.
- **Precision over coverage**: zero false positives > high coverage. ~20% of municipalities need human review.

### Golden Set

24 municipalities verified by hand serve as independent ground truth. The evaluator (`medir_golden_set.py`) measures precision and coverage by portal type after every change.

## Project Structure

```text
scripts/
  crawlers/       Crawlers for bancas, municipios, grounded deep search.
  review/         AI-assisted audit/repair.
  eval/           Golden-set evaluation (medir_golden_set.py).
  shared/         Shared utilities (scope_rs).
  pipeline/       Pipeline orchestration.
config/           Authority matrix, schema, scope rules (YAML).
data/             Golden set, pipeline outputs, seed data.
docs/             Project documentation (architecture, roadmap, decisions).
.github/          CI workflows.
```

## Install

Python 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Environment

Copy `.env.example` to `.env` and fill only the variables needed for the run.

Important variables:
- `GEMINI_API_KEY`: used by Gemini-assisted search/audit.
- `GEMINI_MODEL`: default recommendation is `gemini-2.5-flash`.

Never commit `.env`, API keys, service-account files, model weights, generated logs, or generated exports.

## Common Commands

```bash
# Show crawler help
python scripts/crawlers/crawl_bancas_base_rs.py --help
python scripts/crawlers/crawl_municipios_resources_rs.py --help
python scripts/crawlers/grounded_deepsearch_municipios_a.py --help

# Run grounded deep search sample
python scripts/crawlers/grounded_deepsearch_municipios_a.py --limit 5 --offset 0

# Evaluate against golden set
python scripts/eval/medir_golden_set.py \
  --golden data/golden_set_v1.csv \
  --pipeline <output.csv> --detalle
```

## Verification

- CLI `--help` smoke checks.
- Small limited runs before large crawls.
- Golden-set evaluation with `medir_golden_set.py` (precision + coverage by type).
- Manual inspection in Google Sheets for links and status fields.
