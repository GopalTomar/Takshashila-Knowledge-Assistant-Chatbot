"""
api/main.py — Production HTTP API (FastAPI) for the Takshashila Knowledge Assistant.

One process serves:
  * the public JSON API used by the GitHub Pages frontend  (/api/*)
  * the Mattermost slash command + interactive callbacks    (/mattermost/*)
  * health checks                                           (/health, /api/health)

Both front doors call the SAME engine (src.rag_pipeline / src.retriever).

Access scopes
  * No token  → answers use PUBLIC_SOURCES only (default: the public website).
  * ``Authorization: Bearer <token>`` matching API_ACCESS_TOKENS → INTERNAL_SOURCES
    (Commit KB + website + curated internal files).
  Mattermost requests are authenticated by the slash-command token and always
  use the internal scope.

Operational behaviour
  * X-Request-ID on every response; JSON structured logs (no secrets; query text
    only when LOG_QUERY_TEXT=true).
  * Per-IP rate limit, request timeout, Pydantic validation, CORS allow-list.
  * Errors never leak stack traces: {"error", "message", "request_id"}.
  * KB releases are pulled and hot-swapped by src.kb_sync without downtime.

Run:  uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, List

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src import config
from api.schemas import QueryRequest, QueryResponse

VERSION = "3.0.0"

# ── Structured logging ────────────────────────────────────────────────────────────
log = logging.getLogger("api")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def jlog(event: str, **fields) -> None:
    log.info(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "event": event, **fields}, ensure_ascii=False, default=str))


# ── Readiness ─────────────────────────────────────────────────────────────────────
_ready = {"ok": False, "error": None, "started": time.time(), "warm_seconds": None}


def _warm() -> None:
    t0 = time.perf_counter()
    try:
        from src import kb_sync, vector_store, embeddings
        if kb_sync.enabled() and not config.FAISS_INDEX.exists():
            kb_sync.sync_once(force=True)          # first boot on a fresh container
        vector_store.load_index()
        embeddings._get_model()
        _ready.update(ok=True, error=None, warm_seconds=round(time.perf_counter() - t0, 1))
        jlog("warm_complete", seconds=_ready["warm_seconds"], vectors=vector_store.ntotal())
    except Exception as exc:
        _ready.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        jlog("warm_failed", error=_ready["error"])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from src import kb_sync
    from integrations import mattermost_bot
    mattermost_bot.mattermost_startup(warm=False)   # config checks; warm-up happens below
    from src import vector_store
    mattermost_bot.READINESS_PROBE = lambda: bool(_ready["ok"] and vector_store.is_loaded())
    threading.Thread(target=_warm, name="warm", daemon=True).start()
    kb_sync.start_background_sync()
    yield


app = FastAPI(title="Takshashila Knowledge Assistant API", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")

if config.CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=config.CORS_ALLOW_ORIGINS, allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type",
                                                                "X-Request-ID"],
        expose_headers=["X-Request-ID"], max_age=600)


# ── Middleware: request id + access log ───────────────────────────────────────────
@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:                      # last-resort guard; no stack trace to client
        jlog("unhandled_error", request_id=rid, path=request.url.path, error=type(exc).__name__)
        response = JSONResponse(status_code=500, content={
            "error": "internal_error", "message": "An unexpected error occurred.", "request_id": rid})
    response.headers["X-Request-ID"] = rid
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path not in ("/health", "/api/health"):
        jlog("http", request_id=rid, method=request.method, path=request.url.path,
             status=response.status_code, ms=round((time.perf_counter() - t0) * 1000))
    return response


def _err(request: Request, status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": error, "message": message, "request_id": getattr(request.state, "request_id", "")})


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    msgs = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}"
                     for e in exc.errors()[:5])
    return _err(request, 422, "invalid_request", msgs or "Invalid request.")


@app.exception_handler(HTTPException)
async def _http(request: Request, exc: HTTPException):
    resp = _err(request, exc.status_code, "http_error", str(exc.detail))
    for k, v in (exc.headers or {}).items():
        resp.headers[k] = v
    return resp


# ── Auth scope + rate limiting ────────────────────────────────────────────────────
def access_scope(request: Request) -> Dict:
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token:
        for t in config.API_ACCESS_TOKENS:
            if hmac.compare_digest(token.encode(), t.encode()):
                return {"scope": "internal", "sources": list(config.INTERNAL_SOURCES)}
        raise HTTPException(status_code=401, detail="Invalid access token.")
    return {"scope": "public", "sources": list(config.PUBLIC_SOURCES)}


_hits: Dict[str, Deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limit(request: Request) -> None:
    limit = config.API_RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return
    # The left of X-Forwarded-For is client-controlled (spoofable to dodge the limit);
    # the rightmost entry is the one appended by the platform's edge proxy.
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
          or (request.client.host if request.client else "unknown"))
    now = time.monotonic()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests — please wait a minute.")
        q.append(now)
        if len(_hits) > 10000:                  # bound memory
            for k in list(_hits)[:5000]:
                _hits.pop(k, None)


# ── Health ────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    """Liveness: the process is up (used by Render / Docker health checks)."""
    return {"status": "ok", "service": "takshashila-knowledge-assistant", "version": VERSION}


@app.get("/api/health")
def api_health():
    """Readiness + KB status (no sensitive content)."""
    from src import kb_sync, vector_store
    kb = {"loaded": vector_store.is_loaded(), "vectors": vector_store.ntotal()}
    if vector_store.is_loaded():
        st = vector_store.get_state()
        kb["version"] = st.version
    try:
        from src.refresh import refresh_status
        refresh = refresh_status()
        refresh = {k: refresh.get(k) for k in ("health", "last_success", "last_run_status",
                                              "consecutive_failures", "next_scheduled_run", "timezone")}
    except Exception:
        refresh = {}
    sync = kb_sync.status()
    return {"status": "ok" if _ready["ok"] else ("starting" if not _ready["error"] else "degraded"),
            "ready": _ready["ok"], "error": _ready["error"], "warm_seconds": _ready["warm_seconds"],
            "version": VERSION, "kb": kb, "kb_sync": {k: sync.get(k) for k in (
                "enabled", "last_check", "last_activated", "remote_version", "last_error")},
            "refresh": refresh, "llm_configured": bool(config.GROQ_API_KEY)}


@app.get("/ready")
def ready():
    """Readiness: 200 only when the KB and embedding model are loaded (else 503)."""
    from src import vector_store
    ok = _ready["ok"] and vector_store.is_loaded()     # False during a low-memory KB swap
    body = {"ready": ok, "error": _ready["error"],
            "kb_version": vector_store.get_state().version if vector_store.is_loaded() else None}
    return JSONResponse(status_code=200 if ok else 503, content=body,
                        headers=None if ok else {"Retry-After": "30"})


@app.get("/rag/status")
def rag_status():
    """RAG engine status: KB version/size, bundle sync, daily refresh (no KB content)."""
    return api_health()


def _require_ready(request: Request) -> None:
    from src import vector_store
    if not _ready["ok"] or not vector_store.is_loaded():
        raise HTTPException(status_code=503, detail="The knowledge base is still loading. Try again shortly.",
                            headers={"Retry-After": "30"})


# ── Query ─────────────────────────────────────────────────────────────────────────
def _search_citations(chunks: List[Dict]) -> List[Dict]:
    from src.citation_format import build_citations
    seen, uniq = set(), []
    for c in chunks:
        key = c.get("document_id") or c.get("url")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return build_citations(uniq)


def _run_query(req: QueryRequest, scope: Dict) -> Dict:
    f = req.filters
    allowed = scope["sources"]
    if f.source and f.source.lower() not in allowed:
        return {"answer": "That source is not available with your access level.", "citations": [],
                "confidence": "none", "grounded": False, "timings": {}}
    if req.mode == "search":
        from src.retriever import retrieve
        t0 = time.perf_counter()
        chunks = retrieve(req.query, top_k=max(req.top_k, 8), source=f.source, category=f.category,
                          author=f.author, year=f.year, content_type=f.content_type,
                          allowed_sources=allowed)
        cites = _search_citations(chunks)
        return {"answer": f"Found {len(cites)} matching document(s)." if cites else
                "No matching documents were found.", "citations": cites,
                "confidence": "none", "grounded": bool(cites),
                "timings": {"retrieval_seconds": round(time.perf_counter() - t0, 3)}}
    from src.rag_pipeline import answer
    res = answer(req.query, top_k=req.top_k, source=f.source, category=f.category, author=f.author,
                 year=f.year, content_type=f.content_type, allowed_sources=allowed, mode=req.mode)
    g = res.get("grounding") or {}
    return {"answer": res["answer"], "citations": res.get("citations", []),
            "confidence": res.get("confidence", "none"),
            "grounded": bool(res.get("citations")) and res.get("confidence") != "none",
            "timings": {"retrieval_seconds": round(res.get("retrieval_time") or 0, 3),
                        "generation_seconds": round(res.get("generation_time") or 0, 3)},
            "grounding": {k: g.get(k) for k in ("grounding_score", "claims_total", "claims_supported",
                                               "attribution")},
            "retrieved": len(res.get("chunks") or []), "model": res.get("model")}


@app.post("/api/query", response_model=QueryResponse,
          responses={401: {}, 422: {}, 429: {}, 503: {}, 504: {}})
async def api_query(req: QueryRequest, request: Request, scope=Depends(access_scope),
                    _rl=Depends(rate_limit), _ready_dep=Depends(_require_ready)):
    rid = request.state.request_id
    t0 = time.perf_counter()
    if req.mode != "search" and not config.GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="The answer service is not configured.")
    try:
        out = await asyncio.wait_for(run_in_threadpool(_run_query, req, scope),
                                     timeout=config.API_QUERY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        jlog("query_timeout", request_id=rid)
        raise HTTPException(status_code=504, detail="The request took too long. Please try again.")
    except HTTPException:
        raise
    except Exception as exc:
        jlog("query_error", request_id=rid, error=type(exc).__name__)
        if type(exc).__name__ == "KBReloading":           # low-memory release swap in progress
            raise HTTPException(status_code=503, headers={"Retry-After": "30"},
                                detail="The knowledge base is being updated. Please try again in a minute.")
        if type(exc).__name__ == "RateLimitError":        # LLM provider quota (Groq 429)
            raise HTTPException(status_code=503, headers={"Retry-After": "60"},
                                detail="The AI service is busy right now. Please try again in a minute.")
        raise HTTPException(status_code=502, detail="The answer service is temporarily unavailable.")
    from src import vector_store
    latency = round(time.perf_counter() - t0, 3)
    jlog("query", request_id=rid, scope=scope["scope"], mode=req.mode, latency=latency,
         confidence=out["confidence"], n_citations=len(out["citations"]),
         source_ids=[c.get("document_id") for c in out["citations"]][:10],
         retrieved=out.get("retrieved"), timings=out.get("timings"),
         **({"query": req.query} if config.LOG_QUERY_TEXT else {"query_chars": len(req.query)}))
    meta = {"request_id": rid, "latency_seconds": latency, "scope": scope["scope"],
            "mode": req.mode, "kb_version": vector_store.get_state().version,
            **{k: out.get(k) for k in ("timings", "grounding", "retrieved", "model") if out.get(k) is not None}}
    return {"answer": out["answer"], "citations": out["citations"], "sources": out["citations"],
            "confidence": out["confidence"], "grounded": out["grounded"], "metadata": meta}


# ── Stats / sources / people ──────────────────────────────────────────────────────
@app.get("/api/stats")
def api_stats(scope=Depends(access_scope), _r=Depends(_require_ready)):
    from src import vector_store
    s = vector_store.index_stats()
    if scope["scope"] == "public":
        allowed = set(config.PUBLIC_SOURCES)
        st = vector_store.get_state()
        docs = {m.get("document_id") for m in st.metadata if m.get("source") in allowed}
        types: Dict[str, int] = {}
        for m in st.metadata:
            if m.get("source") in allowed:
                types[m.get("content_type") or "unknown"] = types.get(m.get("content_type") or "unknown", 0) + 1
        return {"scope": "public", "documents": len(docs), "chunks_by_content_type": types,
                "kb_version": s["version"]}
    return {"scope": "internal", **s}


@app.get("/api/sources/{document_id}")
def api_source(document_id: str, scope=Depends(access_scope), _r=Depends(_require_ready)):
    from src import vector_store
    from src.citation_format import build_citations
    chunks = [m for m in vector_store.get_state().metadata if m.get("document_id") == document_id]
    if not chunks or chunks[0].get("source") not in scope["sources"]:
        raise HTTPException(status_code=404, detail="Document not found.")
    rec = build_citations([chunks[0]])[0]
    rec["excerpt"] = " ".join(c.get("text", "") for c in sorted(chunks, key=lambda c: c.get("chunk_index", 0))[:3])[:1500]
    rec["chunks"] = len(chunks)
    return rec


@app.get("/api/people/{name}")
def api_person(name: str, scope=Depends(access_scope), _r=Depends(_require_ready)):
    from src.people import find_person
    p = find_person(name)
    if not p:
        raise HTTPException(status_code=404, detail="Person not found.")
    out = {k: p.get(k) for k in ("name", "slug", "profile_url", "role", "research_areas",
                                  "work_count", "works")}
    # The people graph is built from every source; only list works the caller may see
    # (a byline on a Commit KB page must never reach an anonymous caller).
    from src import vector_store
    hidden = {m.get("document_id") for m in vector_store.get_state().metadata
              if m.get("source") not in scope["sources"]}
    works = [w for w in (out.get("works") or []) if w.get("document_id") not in hidden]
    if len(works) != len(out.get("works") or []):
        out["works"], out["work_count"] = works, len(works)
    return out


@app.get("/api/refresh-status")
def api_refresh_status(scope=Depends(access_scope)):
    """Administrators only (internal token): last/next refresh, health, counts."""
    if scope["scope"] != "internal":
        raise HTTPException(status_code=401, detail="An access token is required.")
    from src.refresh import refresh_status
    return refresh_status()


# ── Mattermost (same engine) ──────────────────────────────────────────────────────
from integrations.mattermost_bot import router as mattermost_router  # noqa: E402

app.include_router(mattermost_router)
