# Concursos Tracker

Concursos Tracker is a data pipeline for discovering and validating public contest and simplified selection process sources in Brazil. The current pilot scope is **Rio Grande do Sul (RS)**.

The project moved from an Ache Concursos-first prototype to an **authority-first** model:

1. Banca organizadora when a banca exists.
2. Prefeitura/official organ page when there is no banca or for municipal follow-up.
3. Diario/FAMURS/publication portals for homologation, convocacao, nomeacao and long-tail administrative events.
4. Radar portals such as Ache Concursos or PCI only as discovery/audit, not as final authority.

## Current Status

- Canonical implementation: `authority_first/`.
- Scope: RS pilot only.
- Main data target: CSV outputs and Google Sheets review copies.
- Current focus: discover official base pages for concursos and processos seletivos, then validate edital page/PDF links.
- Legacy/lab work: `laboratorio/`, old `scripts/`, `data/`, `output/`, `logs/` are reference material and should not be treated as the main source of truth.

## Project Structure

```text
authority_first/        Current canonical pipeline and docs.
authority_first/scripts/crawlers/
                        Crawlers for bancas, municipios and grounded deep search.
authority_first/scripts/review/
                        AI-assisted audit/repair of banca rows.
authority_first/scripts/eval/
                        Golden-set evaluation utilities.
authority_first/cloud_run/
                        Experimental Cloud Run model serving attempts.
laboratorio/            Historical experiments and prototypes.
PLAN_CODEX.md           Older master plan with important project context.
requirements.txt        Python dependencies.
docs/                   Root-level agent/project documentation.
```

## Install

Python 3.12 is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

On the current Windows machine, the available real Python has often been:

```powershell
C:\Users\Luis Santamaria\AppData\Local\Programs\PythonEmbed312\python.exe
```

The Microsoft Store `python.exe` stub may appear in PATH but is not enough for this project.

## Environment

Copy `.env.example` to `.env` and fill only the variables needed for the run.

```powershell
Copy-Item .env.example .env
```

Important variables:

- `GEMINI_API_KEY`: used by Gemini-assisted search/audit.
- `GEMINI_MODEL`: default recommendation is `gemini-2.5-flash`.
- `GOOGLE_APPLICATION_CREDENTIALS`: pending, only needed for automated Google Sheets upload.
- Cloud Run/RunPod variables are experimental and should not be used unless explicitly testing infra.

Never commit `.env`, API keys, service-account files, model weights, generated logs, generated exports, or local assistant state.

## Common Commands

Show crawler help:

```powershell
python authority_first\scripts\crawlers\crawl_bancas_base_rs.py --help
python authority_first\scripts\crawlers\crawl_municipios_resources_rs.py --help
python authority_first\scripts\crawlers\grounded_deepsearch_municipios_a.py --help
```

Run banca discovery for one year:

```powershell
python authority_first\scripts\crawlers\crawl_bancas_base_rs.py --year 2026 --debug
```

Run municipality resources sample:

```powershell
python authority_first\scripts\crawlers\crawl_municipios_resources_rs.py --limit 10 --debug
```

Run grounded deep search sample:

```powershell
python authority_first\scripts\crawlers\grounded_deepsearch_municipios_a.py --limit 5 --offset 0 --ai-route-validator
```

Evaluate against a golden set:

```powershell
python authority_first\scripts\eval\medir_golden_set.py --golden authority_first\data\golden_set_v1.csv --pipeline <output.csv> --detalle
```

## Tests

No formal test suite was found. Current verification is:

- CLI `--help` smoke checks.
- Small limited runs before large crawls.
- Golden-set evaluation with `authority_first/scripts/eval/medir_golden_set.py`.
- Manual inspection in Google Sheets for links and status fields.

Pending: add automated unit tests for URL validation, route selection, and golden-set regression.

## GitHub Status

This folder is being prepared for a private GitHub repository. If Git/GitHub CLI are not installed on the current PC, initialize and push manually after installing them. See `docs/SETUP.md`.
