# Production image: API (/api/*) + Mattermost bot (/mattermost/*) on one port.
#
#   docker build -t takshashila-kb-api .
#   docker run -p 8000:8000 --env-file .env -e PORT=8000 -v "$PWD/data:/app/data" takshashila-kb-api
#
# The image contains NO knowledge-base data and NO secrets. At startup the API
# either uses a KB mounted at /app/data, or downloads the latest encrypted KB
# release (KB_BUNDLE_MANIFEST_URL + KB_BUNDLE_KEY) and keeps it current.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    TOKENIZERS_PARALLELISM=false \
    DATA_DIR=/app/data \
    PORT=8000

WORKDIR /app

# CPU-only torch first (the default wheel bundles CUDA and is several GB larger).
COPY requirements-api.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements-api.txt

# Bake the embedding model so containers start without a Hugging Face download.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBEDDING_MODEL'])" \
 && chmod -R a+rX /opt/hf

# The model is baked in: never contact Hugging Face at runtime.
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

COPY src/ src/
COPY api/ api/
COPY integrations/ integrations/
COPY scripts/ scripts/

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data/logs /app/data/reports /app/data/releases \
 && chown -R app:app /app/data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

# One worker: the model + FAISS index live in memory once. Railway injects $PORT.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 30"]
