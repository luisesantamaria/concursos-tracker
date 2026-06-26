# Instructions for Codex Agents

Read this file before editing the project.

## Project Orientation

- Work from the repository root.
- Treat `authority_first/` as the canonical implementation.
- Treat `laboratorio/`, old `scripts/`, `data/`, `output/`, and `logs/` as legacy/reference unless the user explicitly asks otherwise.
- The pilot scope is RS only. Do not expand to other states unless the user explicitly changes the scope.

## Core Rules

- Do not hardcode one-off URLs just to pass a sample. Generalize the behavior and validate with the golden set.
- Source authority order:
  1. Banca organizadora.
  2. Prefeitura/official organ site.
  3. Diario/FAMURS/publication portals.
  4. Radar portals only for discovery/audit.
- Radar portals such as Ache Concursos, PCI, QConcursos, Folha Dirigida, Estrategia and Gran are not final evidence.
- Prefer precise official pages that aggregate all documents for an event, not a single news article.
- For Fundatec, `pagina_editais.php` is usually the official document base; use `index_concursos.php` only to discover missing abertura PDF links when needed.
- For municipality pages, prefer the best category page, not the first URL that validates. Compare candidate categories such as `Processo Seletivo Simplificado`, `Processo Seletivo`, `Selecao Publica`, and generic `Contratacoes`.

## AI Usage

- Prefer deterministic crawling first, then Gemini as verifier/fallback.
- Use `gemini-2.5-flash` by default. Avoid Flash Lite unless the user asks for cheaper exploratory runs; it previously produced more service errors.
- Keep prompts focused and token use bounded.
- AI can validate and investigate, but official page content remains the source of truth.
- Record why a row is `revisar` or `nao_encontrada`; empty comments make review painful.

## Development

- Use Python 3.12.
- Use `requirements.txt` for dependencies.
- Use `rg` for searches.
- Do not commit generated outputs, logs, caches, credentials, model weights, or local assistant settings.
- If touching crawlers, run a small sample before a broad run.
- If touching validators, run golden-set evaluation.

## Useful Commands

```powershell
python authority_first\scripts\crawlers\crawl_bancas_base_rs.py --help
python authority_first\scripts\crawlers\crawl_municipios_resources_rs.py --help
python authority_first\scripts\crawlers\grounded_deepsearch_municipios_a.py --help
python authority_first\scripts\review\ai_repair_bancas_rs.py --help
python authority_first\scripts\eval\medir_golden_set.py --help
```

## Final Checklist

- Confirm no secrets are staged.
- Confirm generated outputs are ignored.
- Run relevant `--help` or small-run smoke checks.
- If validators changed, run golden-set evaluation.
- Update docs when the pipeline behavior changes.
