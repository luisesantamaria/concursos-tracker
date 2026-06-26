FROM vllm/vllm-openai:latest

ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["python3 -m vllm.entrypoints.openai.api_server --host 0.0.0.0 --port ${PORT:-8080} --model ${VLLM_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ} --served-model-name ${VLLM_SERVED_MODEL_NAME:-concursos-qwen7b} --max-model-len ${VLLM_MAX_MODEL_LEN:-4096} --gpu-memory-utilization ${VLLM_GPU_MEMORY_UTILIZATION:-0.88} --max-num-seqs ${VLLM_MAX_NUM_SEQS:-1} --enforce-eager --disable-log-requests --api-key ${VLLM_API_KEY}"]
