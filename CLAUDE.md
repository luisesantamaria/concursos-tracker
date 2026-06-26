# Claude Code Instructions

This project is Concursos Tracker, an authority-first crawler and validation pipeline for public contests and simplified selection processes in Rio Grande do Sul.

## Context

The project began as an Ache Concursos radar prototype and evolved into a source-authority model. The important principle is:

- Banca pages are authoritative for the active contest lifecycle.
- Prefeitura and Diario/FAMURS pages are authoritative for municipal-only processes and post-result administrative events.
- Radar portals are useful to discover candidates and audit coverage, but they are not final proof.

## Where to Work

- Main code: `authority_first/`.
- Main plans/docs: root docs plus `authority_first/docs/`.
- Legacy experiments: `laboratorio/`, old `scripts/`, generated `data/`, `logs/`, `output/`.

Do not restart from scratch unless the user explicitly asks. Preserve what was learned in the lab, but implement durable logic in `authority_first/`.

## Commands

Install:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

Smoke checks:

```powershell
python authority_first\scripts\crawlers\crawl_bancas_base_rs.py --help
python authority_first\scripts\crawlers\crawl_municipios_resources_rs.py --help
python authority_first\scripts\crawlers\grounded_deepsearch_municipios_a.py --help
python authority_first\scripts\review\ai_repair_bancas_rs.py --help
```

Golden-set evaluation:

```powershell
python authority_first\scripts\eval\medir_golden_set.py --golden authority_first\data\golden_set_v1.csv --pipeline <output.csv> --detalle
```

## Pipeline Rules

- Scope remains RS only.
- Do not accept federal/national contests only because tests happen in RS.
- For statewide contests, set municipality as `Estatal`.
- Keep `concurso_publico` and `processo_seletivo` separate.
- The desired base fields are: `tipo`, `orgao`, `municipio`, `uf`, `numero`, `banca`, `edital_pagina`, `edital_pdf`.
- A row should be `listo` only when page, PDF, number, year and identity match.
- If a row is `revisar`, explain why.
- Do not use a specific official URL from a conversation as a patch unless it teaches a general rule.

## Known Tricky Sources

- Fundatec: abertura PDFs can appear from `index_concursos.php`, while the document base is `pagina_editais.php`.
- Legalle has at least two portals: `portal.editais.legalleconcursos.com.br` and `portal.institutolegalle.org.br`.
- La Salle can throttle or block if crawled too aggressively.
- FAURGS and some municipal sites use buttons/download handlers that may require deeper link extraction.
- Municipality menus can hide process pages under `Editais`, `Publicacoes`, `Transparencia`, `Concursos`, or external transparency systems.

## Safety

Never commit `.env`, local API keys, generated outputs, logs, model files, Cloud Run/RunPod secrets, or `.claude/settings.local.json`.
