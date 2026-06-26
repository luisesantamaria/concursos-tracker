# Setup

## 1. Prerequisites

- Python 3.12.
- Git.
- Optional: GitHub CLI (`gh`) for private repo creation.
- Optional: Gemini API key for AI-assisted search/audit.

## 2. Create Environment

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
# .\.venv\Scripts\Activate.ps1  # Windows PowerShell
pip install -r requirements.txt
playwright install chromium
```

## 3. Configure Environment Variables

```bash
cp .env.example .env
```

Fill only what you need:

```text
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
```

Do not commit `.env`.

## 4. Smoke Checks

```bash
python authority_first/scripts/crawlers/crawl_bancas_base_rs.py --help
python authority_first/scripts/crawlers/crawl_municipios_resources_rs.py --help
python authority_first/scripts/crawlers/grounded_deepsearch_municipios_a.py --help
python authority_first/scripts/review/ai_repair_bancas_rs.py --help
python authority_first/scripts/eval/medir_golden_set.py --help
```

## 5. Small Runs

```bash
# Banca sample
python authority_first/scripts/crawlers/crawl_bancas_base_rs.py --year 2026 --max-total 50 --debug

# Municipality resources sample
python authority_first/scripts/crawlers/crawl_municipios_resources_rs.py --limit 10 --debug

# Grounded deep search sample
python authority_first/scripts/crawlers/grounded_deepsearch_municipios_a.py --limit 5 --offset 0
```

## 6. Golden-Set Evaluation

```bash
python authority_first/scripts/eval/medir_golden_set.py \
  --golden authority_first/data/golden_set_v1.csv \
  --pipeline <output.csv> \
  --detalle
```

The `--detalle` flag shows per-municipality verdict breakdown. Run after any change to verification or selection logic.

## 7. Troubleshooting

- Playwright errors: run `playwright install chromium`.
- Rate limits/blocks: lower concurrency and host delay; La Salle/Wordfence is sensitive.
- Gemini errors: retry with `gemini-2.5-flash`; avoid Flash Lite for critical review runs.
- HTTP 406 from municipal sites: ensure requests sessions use real browser User-Agent headers, not bot-like identifiers.

## Pending

- Official command for Google Sheets upload.
- Formal automated tests.
- Final production storage layout.
