#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-concursos-containers}"
SERVICE="${SERVICE:-concursos-ai-review}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/llama-cpp-qwen25-7b:server-cuda"
SECRET="${SECRET:-concursos-ai-review-api-key}"
MODEL_REPO="${MODEL_REPO:-bartowski/Qwen2.5-7B-Instruct-GGUF}"
MODEL_FILE="${MODEL_FILE:-Qwen2.5-7B-Instruct-Q4_K_M.gguf}"
ALIAS="${ALIAS:-concursos-qwen7b}"

echo "[cloud-run] project=${PROJECT_ID}"
echo "[cloud-run] region=${REGION}"
echo "[cloud-run] service=${SERVICE}"
echo "[cloud-run] image=${IMAGE}"
echo "[cloud-run] model_repo=${MODEL_REPO}"
echo "[cloud-run] model_file=${MODEL_FILE}"

if ! gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1; then
  echo "[cloud-run] creating Artifact Registry repo ${REPO}"
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Concursos AI review containers" \
    --async
fi

echo "[cloud-run] building/pushing llama.cpp CUDA base image"
gcloud builds submit . --tag "${IMAGE}"

if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  echo "[cloud-run] creating API key secret ${SECRET}"
  openssl rand -hex 32 | gcloud secrets create "${SECRET}" \
    --replication-policy="automatic" \
    --data-file=-
fi

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")"
SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

echo "[cloud-run] deploying GPU service with min-instances=0 max-instances=1"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --cpu 4 \
  --memory 16Gi \
  --no-cpu-throttling \
  --no-gpu-zonal-redundancy \
  --concurrency 1 \
  --max-instances 1 \
  --min-instances 0 \
  --timeout 3600 \
  --allow-unauthenticated \
  --set-env-vars "LLAMA_ARG_HF_REPO=${MODEL_REPO},LLAMA_ARG_HF_FILE=${MODEL_FILE},LLAMA_ARG_ALIAS=${ALIAS},LLAMA_ARG_HOST=0.0.0.0,LLAMA_ARG_PORT=8080,LLAMA_ARG_N_GPU_LAYERS=999,LLAMA_ARG_CTX_SIZE=4096,LLAMA_ARG_N_PARALLEL=1,LLAMA_ARG_N_PREDICT=768,LLAMA_ARG_CONT_BATCHING=true,LLAMA_ARG_UI=false,LLAMA_ARG_LOG_TIMESTAMPS=true" \
  --set-secrets "LLAMA_ARG_API_KEY=${SECRET}:latest" \
  --quiet

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
API_KEY="$(gcloud secrets versions access latest --secret "${SECRET}")"

echo
echo "[cloud-run] ready"
echo "CLOUD_RUN_BASE_URL=${URL}/v1"
echo "CLOUD_RUN_MODEL=${ALIAS}"
echo "CLOUD_RUN_API_KEY=${API_KEY}"
echo
echo "Delete service when done:"
echo "gcloud run services delete ${SERVICE} --region ${REGION} --quiet"
