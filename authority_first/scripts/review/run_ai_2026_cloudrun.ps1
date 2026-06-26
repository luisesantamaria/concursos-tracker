$ErrorActionPreference = "Stop"

$Project = "C:\Users\Luis Santamaria\iCloudDrive\Desktop\Projects\Concursos Tracker"
$Python = "C:\Users\Luis Santamaria\AppData\Local\Programs\PythonEmbed312\python.exe"

$BaseUrl = [Environment]::GetEnvironmentVariable("CLOUD_RUN_BASE_URL", "User")
$ApiKey = [Environment]::GetEnvironmentVariable("CLOUD_RUN_API_KEY", "User")
$Model = [Environment]::GetEnvironmentVariable("CLOUD_RUN_MODEL", "User")

if (-not $BaseUrl) { throw "CLOUD_RUN_BASE_URL is missing in User env. Example: https://SERVICE.run.app/v1" }
if (-not $ApiKey) { throw "CLOUD_RUN_API_KEY is missing in User env." }
if (-not $Model) { $Model = "concursos-qwen7b" }

$env:CLOUD_RUN_API_KEY = $ApiKey

$InputCsv = "$Project\authority_first\data\exports\bancas_base_rs_2026_quick_audit_v2.csv"
$OutReview = "$Project\authority_first\data\exports\bancas_base_rs_2026_ai_full_review_cloudrun.csv"
$OutApplied = "$Project\authority_first\data\exports\bancas_base_rs_2026_ai_full_applied_cloudrun.csv"
$CacheDir = "$Project\authority_first\data\cache\ai_review_2026_cloudrun"
$Log = "$Project\authority_first\data\logs\ai_cloudrun_2026.log"

New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
if (Test-Path $Log) { Remove-Item -LiteralPath $Log -Force }

& $Python "$Project\authority_first\scripts\review\ai_repair_bancas_rs.py" `
    --input $InputCsv `
    --out-review $OutReview `
    --out-applied $OutApplied `
    --only revisar `
    --llm-provider openai `
    --openai-base-url $BaseUrl `
    --openai-model $Model `
    --openai-api-key-env CLOUD_RUN_API_KEY `
    --openai-timeout 3600 `
    --num-ctx 4096 `
    --num-predict 520 `
    --debug `
    --cache-dir $CacheDir `
    --max-pages-per-row 6 `
    --max-docs-per-row 12 `
    --max-raw-docs-per-row 60 `
    --max-pdf-texts-per-row 4 `
    --page-text-chars 900 `
    --pdf-text-chars 1400 `
    --pdf-pages 2 `
    --timeout 30 `
    --retries 1 `
    --host-delay 0.10 `
    --lasalle-host-delay 1.0 2>&1 | Tee-Object -FilePath $Log -Append

Write-Host "OUT_REVIEW=$OutReview"
Write-Host "OUT_APPLIED=$OutApplied"
Write-Host "LOG=$Log"
