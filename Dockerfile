# Production image: API (/api/*) + Mattermost bot (/mattermost/*) on one port.
# Built and run by Render (render.yaml); works on any Docker host.
#
#   docker build -t takshashila-kb-api .
#   docker run -p 8000:8000 --env-file .env -e PORT=8000 takshashila-kb-api
#
# The image contains NO knowledge-base data and NO secrets. At startup the API
# downloads the latest encrypted KB release published by GitHub Actions
# (KB_BUNDLE_MANIFEST_URL + KB_BUNDLE_KEY), verifies it, and keeps it current.
#
# Two stages keep the runtime small enough for a 512 MB instance (Render Free):
#   1. "embedder": torch + sentence-transformers export the embedding model to ONNX
#      and verify the ONNX vectors match sentence-transformers (build fails if not).
#   2. runtime: onnxruntime only — no torch in the serving process. The model weights
#      and (with KB_LOW_MEMORY) the FAISS vectors are memory-mapped; a fixed glibc
#      mmap threshold stops large temporary buffers from fragmenting the heap.

# ── Stage 1: export the embedding model to ONNX ───────────────────────────────────
FROM python:3.12-slim AS embedder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/tmp/hf HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
WORKDIR /build
COPY requirements-api.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements-api.txt "sentence-transformers>=3.0" onnx onnxscript

ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
COPY src/ src/
COPY scripts/export_onnx_embedder.py scripts/
RUN python scripts/export_onnx_embedder.py --model "${EMBEDDING_MODEL}" --out /embedder

# ── Stage 2: runtime ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TOKENIZERS_PARALLELISM=false \
    MALLOC_ARENA_MAX=2 \
    MALLOC_MMAP_THRESHOLD_=131072 \
    DATA_DIR=/app/data \
    EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    EMBEDDING_BACKEND=onnx \
    EMBEDDING_ONNX_DIR=/app/models/embedder \
    HF_HUB_OFFLINE=1

WORKDIR /app
COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY --from=embedder /embedder /app/models/embedder
COPY src/ src/
COPY api/ api/
COPY integrations/ integrations/
COPY scripts/ scripts/

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data/logs /app/data/reports /app/data/releases \
 && chown -R app:app /app/data \
 && chmod -R a+rX /app/models
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

# One worker: the model + FAISS index live in memory once. The host injects $PORT
# (Render: 10000); 8000 is only the fallback for a plain `docker run`.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 30"]
