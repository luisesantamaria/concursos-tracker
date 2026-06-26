# Cloud Run llama.cpp Qwen 7B

Self-hosted OpenAI-compatible review backend for `ai_repair_bancas_rs.py`.

This deploys `ghcr.io/ggml-org/llama.cpp:server-cuda` on Cloud Run with one NVIDIA L4, `min-instances=0`, `max-instances=1`, and an application API key stored in Secret Manager.

The model is loaded from Hugging Face on cold start:

`bartowski/Qwen2.5-7B-Instruct-GGUF` / `Qwen2.5-7B-Instruct-Q4_K_M.gguf`

That keeps the image small for the first validation pass. If cold starts are too expensive, the next optimization is baking the GGUF into the image or using a persistent model cache strategy.

## Deploy from Cloud Shell

```bash
cd ~/cloudrun-llama-qwen7b
bash deploy_cloud_shell.sh
```

The script prints:

- `CLOUD_RUN_BASE_URL`
- `CLOUD_RUN_MODEL`
- `CLOUD_RUN_API_KEY`

Set those as Windows user env vars before running:

```powershell
[Environment]::SetEnvironmentVariable("CLOUD_RUN_BASE_URL", "https://SERVICE.run.app/v1", "User")
[Environment]::SetEnvironmentVariable("CLOUD_RUN_MODEL", "concursos-qwen7b", "User")
[Environment]::SetEnvironmentVariable("CLOUD_RUN_API_KEY", "PASTE_KEY", "User")
```

Then:

```powershell
& "C:\Users\Luis Santamaria\iCloudDrive\Desktop\Projects\Concursos Tracker\authority_first\scripts\review\run_ai_2026_cloudrun.ps1"
```

## Delete service

```bash
gcloud run services delete concursos-ai-review --region us-central1 --quiet
```
