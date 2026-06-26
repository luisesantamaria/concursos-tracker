# Setup

## 1. Prerequisites

- Windows PowerShell or a Unix-like shell.
- Python 3.12.
- Git.
- Optional: GitHub CLI (`gh`) for private repo creation.
- Optional: Google/Gemini API key for AI-assisted search/review.

## 2. Clone or Open the Project

```powershell
cd "C:\path\to\Concursos Tracker"
```

If using the current local folder:

```powershell
cd "C:\Users\Luis Santamaria\Documents\PC\iCloud_Duplicate_Cleanup_Hold\Concursos Tracker local-only pending 20260625-145747"
```

## 3. Create Environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

If `python` opens the Microsoft Store or fails, use the real installed Python path or install Python 3.12.

Known local path on this PC:

```powershell
& "C:\Users\Luis Santamaria\AppData\Local\Programs\PythonEmbed312\python.exe" --version
```

## 4. Configure Environment Variables

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill only what you need:

```text
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
```

Do not commit `.env`.

## 5. Smoke Checks

```powershell
python authority_first\scripts\crawlers\crawl_bancas_base_rs.py --help
python authority_first\scripts\crawlers\crawl_municipios_resources_rs.py --help
python authority_first\scripts\crawlers\grounded_deepsearch_municipios_a.py --help
python authority_first\scripts\review\ai_repair_bancas_rs.py --help
python authority_first\scripts\eval\medir_golden_set.py --help
```

## 6. Small Runs

Banca sample:

```powershell
python authority_first\scripts\crawlers\crawl_bancas_base_rs.py --year 2026 --max-total 50 --debug
```

Municipio resources sample:

```powershell
python authority_first\scripts\crawlers\crawl_municipios_resources_rs.py --limit 10 --debug
```

Grounded deep search sample:

```powershell
python authority_first\scripts\crawlers\grounded_deepsearch_municipios_a.py --limit 5 --offset 0 --ai-route-validator
```

## 7. Golden-Set Evaluation

```powershell
python authority_first\scripts\eval\medir_golden_set.py --golden authority_first\data\golden_set_v1.csv --pipeline <output.csv> --detalle
```

## 8. GitHub Private Repo

If Git and GitHub CLI are installed and authenticated:

```powershell
git init
git checkout -B main
git add .
git commit -m "Initial project setup with agent docs"
gh repo create "$(Split-Path -Leaf (Get-Location))" --private --source=. --remote=origin --push
```

If the folder name is too long or awkward for GitHub, use:

```powershell
gh repo create "Concursos-Tracker" --private --source=. --remote=origin --push
```

If a remote already exists:

```powershell
git push -u origin main
```

## 9. Troubleshooting

- Store Python stub: install Python 3.12 or call the real executable path.
- Playwright errors: run `playwright install chromium`.
- Rate limits/blocks: lower concurrency and host delay; La Salle/Wordfence is sensitive.
- iCloud sync issues: work from the local backup folder until sync is stable.
- Gemini errors: retry with `gemini-2.5-flash`; avoid Flash Lite for critical review runs.

## Pending

- Official command for Google Sheets upload.
- Formal automated tests.
- Final production storage layout.
