#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-concursos-containers}"
SERVICE="${SERVICE:-concursos-ai-review}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/vllm-qwen25-7b-awq:latest"
SECRET="${SECRET:-concursos-ai-review-api-key}"
MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
ALIAS="${ALIAS:-concursos-qwen7b}"

echo "[vllm] project=${PROJECT_ID}"
echo "[vllm] region=${REGION}"
echo "[vllm] service=${SERVICE}"
echo "[vllm] image=${IMAGE}"
echo "[vllm] model=${MODEL}"

echo "[vllm] deleting old service if present, to stop crash loops"
gcloud run services delete "${SERVICE}" --region "${REGION}" --quiet >/dev/null 2>&1 || true

echo "[vllm] building/pushing vLLM image"
gcloud builds submit . --tag "${IMAGE}"

if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  echo "[vllm] creating API key secret ${SECRET}"
  openssl rand -hex 32 | gcloud secrets create "${SECRET}" \
    --replication-policy="automatic" \
    --data-file=-
else
  echo "[vllm] rotating API key secret ${SECRET}"
  openssl rand -hex 32 | gcloud secrets versions add "${SECRET}" \
    --data-file=-
fi

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")"
SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

echo "[vllm] deploying GPU service with min-instances=0 max-instances=1"
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
  --port 8080 \
  --allow-unauthenticated \
  --set-env-vars "VLLM_MODEL=${MODEL},VLLM_SERVED_MODEL_NAME=${ALIAS},VLLM_MAX_MODEL_LEN=4096,VLLM_GPU_MEMORY_UTILIZATION=0.88,VLLM_MAX_NUM_SEQS=1" \
  --set-secrets "VLLM_API_KEY=${SECRET}:latest" \
  --quiet

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
API_KEY="$(gcloud secrets versions access latest --secret "${SECRET}")"

echo
echo "[vllm] ready"
echo "CLOUD_RUN_BASE_URL=${URL}/v1"
echo "CLOUD_RUN_MODEL=${ALIAS}"
echo "CLOUD_RUN_API_KEY=${API_KEY}"
echo
echo "Delete service when done:"
echo "gcloud run services delete ${SERVICE} --region ${REGION} --quiet"
