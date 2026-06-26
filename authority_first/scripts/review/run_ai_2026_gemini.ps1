$ErrorActionPreference = "Stop"

$Project = "C:\Users\Luis Santamaria\iCloudDrive\Desktop\Projects\Concursos Tracker"
$Python = "C:\Users\Luis Santamaria\AppData\Local\Programs\PythonEmbed312\python.exe"

$ApiKey = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "User")
if (-not $ApiKey) { $ApiKey = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "Process") }
if (-not $ApiKey) { throw "GEMINI_API_KEY is missing. Set it as a User env var or current process env var." }
$env:GEMINI_API_KEY = $ApiKey

$Model = [Environment]::GetEnvironmentVariable("GEMINI_MODEL", "User")
if (-not $Model) { $Model = "gemini-2.5-flash-lite" }

$InputCsv = "$Project\authority_first\data\exports\bancas_base_rs_2026_quick_audit_v2.csv"
$OutReview = "$Project\authority_first\data\exports\bancas_base_rs_2026_ai_review_gemini.csv"
$OutApplied = "$Project\authority_first\data\exports\bancas_base_rs_2026_ai_applied_gemini.csv"
$CacheDir = "$Project\authority_first\data\cache\ai_review_2026_gemini"
$Log = "$Project\authority_first\data\logs\ai_gemini_2026.log"

New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
if (Test-Path $Log) { Remove-Item -LiteralPath $Log -Force }

& $Python "$Project\authority_first\scripts\review\ai_repair_bancas_rs.py" `
    --input $InputCsv `
    --out-review $OutReview `
    --out-applied $OutApplied `
    --only revisar `
    --llm-provider gemini `
    --gemini-model $Model `
    --gemini-api-key-env GEMINI_API_KEY `
    --gemini-timeout 180 `
    --num-ctx 4096 `
    --num-predict 700 `
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
