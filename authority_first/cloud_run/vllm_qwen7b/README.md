# Cloud Run vLLM Qwen 7B

OpenAI-compatible Cloud Run GPU backend for `ai_repair_bancas_rs.py`.

This is the fallback after `llama.cpp:server-cuda` hit a CUDA warmup crash on Cloud Run L4:

`CUDA error: device kernel image is invalid`

The service uses:

- `vllm/vllm-openai:latest`
- `Qwen/Qwen2.5-7B-Instruct-AWQ`
- one NVIDIA L4
- `min-instances=0`
- `max-instances=1`
- `concurrency=1`

Deploy from Cloud Shell:

```bash
mkdir -p ~/cloudrun-vllm-qwen7b
cd ~/cloudrun-vllm-qwen7b
# create Dockerfile and deploy_cloud_shell.sh, then:
bash deploy_cloud_shell.sh
```

The script prints the three values needed by:

`authority_first/scripts/review/run_ai_2026_cloudrun.ps1`
