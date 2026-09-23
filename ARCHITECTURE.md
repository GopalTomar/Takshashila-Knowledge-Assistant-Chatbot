# Architecture — Takshashila Knowledge Assistant

This describes the system **as implemented** on the `production-hardening` branch.

## 1. Production topology

```
                      ┌─────────────────────────── GitHub (public repo) ────────────────────────────┐
 Users (browser) ───► │ GitHub Pages: frontend/ (static HTML/CSS/JS, no secrets)                      │
                      │                                                                               │
                      │ Actions: kb-refresh.yml  (polls every 30 min; gate = 06:00 Asia/Kolkata)      │
                      │   restore last KB ─► crawl website + Commit KB ─► incremental embed ─►        │
                      │   FAISS/BM25 ─► validate + smoke ─► publish ENCRYPTED bundle to release       │
                      │   "kb-latest" (kb-manifest.json + kb-<version>.tkkb + kb-status.json)         │
                      └───────────────┬───────────────────────────────────────────────▲──────────────┘
                                      │ HTTPS  POST /api/query (CORS allow-list)       │ poll manifest,
                                      ▼                                                │ download bundle
                      ┌──────────────────────────── Railway (Docker) ──────────────────┴─────────────┐
 Mattermost ────────► │ api.main:app (uvicorn, 1 worker, $PORT)                                       │
 /askkb slash cmd     │   /health, /api/health, /api/query, /api/stats, /api/sources/{id},            │
 + button callbacks   │   /api/people/{name}, /api/refresh-status, /mattermost/*                      │
                      │   src.kb_sync: verify sha256 → decrypt → extract → build state → smoke →      │
                      │                atomic in-memory swap (no downtime; failure keeps old KB)      │
                      │   ONE engine: src.rag_pipeline → src.retriever → src.vector_store (FAISS+BM25) │
                      │               → Groq LLM → src.citations (claim-level verification)           │
                      └───────────────────────────────────────────────────────────────────────────────┘
```

Nothing runs on a laptop. The refresh job and the API are separate processes;
the API never crawls, the crawler never serves traffic.

## 2. Request lifecycle

```
user question
  → frontend (app.js) / Mattermost (/mattermost/ask) / Streamlit (app.py)
  → api.main: request id, rate limit, Pydantic validation, access scope
       (no token → PUBLIC_SOURCES = website; Bearer staff token or Mattermost → INTERNAL_SOURCES)
  → rag_pipeline.answer(query, allowed_sources, filters, mode)
      → retriever.retrieve
          1. FAISS (exact cosine, bge-small-en-v1.5, filters applied, scans all rows when filtered)
          2. BM25 (punctuation-free tokens, same filters)
          3. people graph: named person → extra author-filtered FAISS+BM25 lists
          4. Reciprocal Rank Fusion (k=60)
          5. boosts: source priority, exact title (×1.6), exact URL (×2), named author (×1.35)
          6. evidence pages first, ≤2 chunks/document, deterministic tie-break
          7. true cosine for every returned chunk (BM25-only hits re-scored)
      → evidence gate (best cosine ≥ MIN_SCORE_THRESHOLD) else insufficient-evidence reply
      → drop navigation/listing pages + below-floor chunks; merge chunks per document
      → numbered context with metadata header (title/author/date/type/publisher/url)
      → Groq (timeout + retries)
      → citations.verify: normalise [Source 1, 2] forms; remove fabricated numbers;
          per-sentence support check against the cited passage; renumber;
          ungrounded → insufficient-evidence reply
      → citation_format.build_citations (title, type, authors, date, publisher, url,
          best-matching excerpt, public/internal)
  → JSON {answer, citations, sources, confidence, grounded, metadata{request_id,
          latency, scope, kb_version, timings, grounding}}
```

## 3. Ingestion lifecycle (daily refresh)

```
data/releases/CURRENT ──► active release R(n)
refresh (src/refresh.py):
  copy R(n) → staging R(n+1)       (processed/, index/ incl. embedding cache, state/)
  crawl_engine (per source, isolated):
     robots.txt → sitemap(s) + lastmod → seeds + known URLs → BFS internal links
     canonicalise URLs; shared rate limiter; retries w/ backoff; conditional GET
     Quarto-aware extraction (site_extract): title, lede, AUTHOR links, DATE,
       DOCUMENT series, VERSION, CATEGORIES, sectioned body, figures, links
     team profiles → role, bio, research areas, listed works
     /pages/news/ → one "op-ed" document per listed article
     PDFs: byte hash → text + embedded PDF metadata
     change = content hash (authoritative) → new / modified / unchanged
     temporary failure → keep doc (unavailable_since); 404/unlinked → missing_count;
     removal only after KB_REMOVAL_CONFIRMATIONS complete crawls & failure ratio ok;
     redirect → document moves to final URL
  merge_documents (by document_id; URL collapse)
  rebuild_index: normalise (schema) + supplementary files (holiday list)
     → section-aware chunking with metadata propagation
     → EmbeddingCache: embed only chunks whose content hash is new
     → FAISS IndexFlatIP + metadata.json (atomic temp+rename) → people.json → kb_manifest.json
  validate_kb (critical checks + regression guards vs R(n)) + smoke_test (real retrieval)
  PASS → os.replace(CURRENT := R(n+1))       FAIL → rename failed-R(n+1); CURRENT unchanged
  reports → data/reports/daily_refresh/{latest,YYYY-MM-DD,status}.json, reports/crawl/*
```

## 4. Module map

| Layer | Module | Responsibility |
|---|---|---|
| Config | `src/config.py` | env-driven settings; switchable KB root (`use_kb_root`, `resolve_active_kb_root`) |
| Crawl | `scripts/crawl_engine.py` | discovery, fetching, change detection, audit log |
| | `src/url_utils.py` | URL canonicalisation / classification |
| | `src/site_extract.py` | Quarto-aware extraction (pages, profiles, listings) |
| | `src/crawl_state.py` | per-URL incremental state (hashes, ETags, children, missing counts) |
| | `src/extractors.py` | PDF text + PDF metadata |
| Schema | `src/metadata.py` | content types, date/author normalisation, canonical document schema |
| Index | `src/chunker.py` | section-aware chunks with propagated metadata |
| | `src/embeddings.py`, `src/embedding_cache.py` | bge-small embeddings; content-hash cache |
| | `src/incremental_index.py` | merge, rebuild, manifest |
| | `src/people.py` | person ↔ works graph, query-time person detection |
| | `src/supplementary.py` | curated internal files (holiday list) |
| Serve | `src/vector_store.py` | immutable KBState (FAISS+metadata+BM25), filters, atomic swap |
| | `src/retriever.py` | hybrid retrieval, RRF, boosts, diversity |
| | `src/rag_pipeline.py`, `src/groq_client.py` | prompting, evidence gate, LLM |
| | `src/citations.py`, `src/citation_format.py` | verification; shared citation records |
| Ops | `src/refresh.py`, `scripts/refresh_kb.py`, `scripts/refresh_gate.py` | the single refresh implementation, CLI, tz gate |
| | `src/kb_bundle.py`, `scripts/kb_release.py`, `src/kb_sync.py` | encrypted distribution; API hot-swap |
| | `scripts/validate_kb.py`, `scripts/audit_website.py` | validation; broken-link/crawl QA |
| Interfaces | `api/main.py` | production API + Mattermost router |
| | `integrations/*` | Mattermost bot (signed callbacks, routing, formatting) |
| | `frontend/` | GitHub Pages UI |
| | `app.py` | Streamlit dashboard (admin tabs gated) |

## 5. Data layout

```
data/
  knowledge_base/holiday_list_2026.jsonl     curated input (tracked)
  releases/CURRENT                           name of the active release
  releases/<version>/                        one complete KB (never modified after promotion)
      processed/documents.jsonl, chunks.jsonl, people.json
      index/faiss.index, metadata.json, embedding_cache.npz
      state/website_crawl_state.json, commit_kb_crawl_state.json
      kb_manifest.json
  reports/daily_refresh/{latest,<date>,status}.json
  reports/crawl/{website,commit_kb}_latest.json
  reports/website_audit.*, broken_links.csv, redirects.csv, orphan_pages.csv, crawl_summary.json
```
The legacy flat layout (`data/processed`, `data/index`) is still readable when no
`CURRENT` pointer exists.

## 6. Security model

* Commit KB content never enters git or the public site. It travels only inside
  AES-256-GCM encrypted bundles (key in GitHub secrets + Railway).
* Public API callers get website-only answers; staff tokens unlock internal sources.
* Mattermost: slash token (constant-time), HMAC-signed button contexts and
  dialog state (bound to the channel), post/channel check before deletions.
* No pickle loading by default; tar-slip-safe bundle extraction; HTML escaping in
  Streamlit and the frontend; CSP on the frontend; container runs as non-root.
