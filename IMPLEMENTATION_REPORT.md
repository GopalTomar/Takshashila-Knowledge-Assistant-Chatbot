# Implementation report — production hardening

> **Deployment update (2026-09-24):** the backend now runs on **Render Free** instead of Railway — see `DEPLOYMENT.md`. Railway references below are historical.


Branch `production-hardening` (from baseline tag `baseline-pre-audit`). Nothing has
been pushed. Findings referenced as S/C/R/X/P/D/T are defined in AUDIT_REPORT.md.

## 1. What was wrong (summary)

Public repo exposing the internal Commit KB (S1); unauthenticated Mattermost
callbacks that could post internal answers anywhere or delete bot posts (S2); `.env`
baked into Docker images (S3); pickle loading (S4); 618 polluted author fields (C2);
no op-eds (C3) and no usable team profiles (C4); single-miss deletions (C5); URL
identity bugs (C6, 37 duplicate identities); fabricated URL-based categories (C7);
BM25 ignoring filters (R1); relevant short chunks never retrievable (R2); citations
treated as grounded without checking support (X1) and blind attribution (X2);
in-place index rebuilds without validation (P1); laptop-dependent weekly refresh (D1);
no API/frontend/hosting (D2); broken and very slow tests (T1, P3).

## 2. What was fixed / added

### Crawling & ingestion
* **Crawl engine rewritten** (`scripts/crawl_engine.py`): robots + recursive sitemaps
  with lastmod, canonical URLs, global rate limiter, retries with backoff + jitter +
  Retry-After, conditional GET, redirect handling, per-URL audit log, entry-point
  abort, PDF byte-hash change detection, PDFs re-checked even under 304 parents, all
  known URLs re-checked each run, op-ed listing ingestion, safe removals (complete
  crawl + failure ratio + 2 confirmations + a direct 404/410 check — live-but-unlinked
  documents are never removed), document-ID retirement when a URL's ID changes.
* **Quarto-aware extraction** (`src/site_extract.py`): details table (authors +
  profile URLs, date, series, version, categories), sectioned body, figures, links,
  profile role/bio/research areas/works, listing entries (scheme-less links fixed).
* **Schema** (`src/metadata.py`): 30+ field canonical schema, 20+ content types from
  the live IA, date normalisation, author cleaning, no fabricated values, relative paths.
* **People graph** (`src/people.py`): person ↔ works from bylines and profiles; used at
  query time; dead profile links not adopted.
* **Supplementary files** (`src/supplementary.py`): holiday list now indexed.
* **Cleaning**: cross-document boilerplate removal before chunking (e.g. the
  "About Takshashila" paragraph at the end of every PDF), keeping it on about pages.
* **Chunking**: section-aware (`heading_path`), metadata propagated to every chunk.

### Index, refresh & distribution
* **Versioned releases + atomic promotion** (`src/refresh.py`): staging copy → crawl
  → merge → cache-backed re-embedding → FAISS/metadata written atomically → people
  graph → manifest (with pipeline version) → validation with regression guards →
  smoke tests → single `os.replace` of `data/releases/CURRENT`. Failures leave the
  current release untouched; reports + consecutive-failure health.
* **One CLI** (`scripts/refresh_kb.py`): `--all/--website/--commit-kb`, `--full`,
  `--dry-run`, `--validate-only`, `--rebuild-only`, `--audit-only`, `--status`, `--seed`.
* **Timezone-correct schedule** (`scripts/refresh_gate.py`, GitHub Actions
  `kb-refresh.yml`): 06:00 Asia/Kolkata, once per local day, never UTC.
* **Embedding checkpointing**: blocks of 2,048 with cache saves (crash-resumable).
* **Pipeline version**: processing-code changes trigger a cache-backed re-index.
* **Encrypted distribution** (`src/kb_bundle.py`, `scripts/kb_release.py`): AES-256-GCM,
  block-indexed AAD, sha256 manifest, tar-slip-safe extraction.
* **API hot-swap** (`src/kb_sync.py`): download → verify → decrypt → build state →
  smoke → atomic in-memory swap; failures keep the serving KB.
* **Validation** (`scripts/validate_kb.py`): duplicate IDs/URLs, missing URL/text,
  citation metadata, orphans, mojibake/OCR ratios, index↔metadata, embedding dimension,
  BM25 build, per-source and total regression guards.
* **Website audit** (`scripts/audit_website.py`): broken links, redirects, orphans,
  sitemap-only / links-only, thin pages, missing metadata, duplicates; CSV + JSON.
* **Legacy migration** (`scripts/migrate_legacy_kb.py`): first clean release built from
  a fresh crawl with legacy URLs as extra seeds; reconciliation report for every legacy
  document not carried over.

### Retrieval & answers
* Immutable `KBState` (FAISS + metadata + BM25) with atomic swap; JSON-only metadata.
* Filters (source/category/author/year/content type) on both retrievers; access scope.
* Punctuation-free BM25; true cosine for BM25-only hits; people-aware candidates;
  title/URL/author/content-type-intent boosts; ≤2 chunks per document; deterministic.
* Claim-level citation verification; grouped / bare / full-width (`【Source 1】`) forms
  normalised; fabricated numbers and unsupported citations removed; refusal when
  ungrounded; no blind attribution; shared `build_citations` records with excerpts.
* Groq timeout + retries; interface-neutral insufficient-evidence reply.

### Interfaces & deployment
* **Production API** (`api/main.py`): `/health`, `/api/health`, `/api/query`,
  `/api/stats`, `/api/sources/{id}`, `/api/people/{name}`, `/api/refresh-status`,
  Mattermost routes; Pydantic models, request IDs, JSON logs, CORS allow-list,
  per-IP rate limit, request timeout, structured errors, public vs staff scope.
* **Mattermost**: router mounted in the API; signed callbacks; channel-bound dialog
  state; delete verification; constant-time token check; source cards with type,
  author, date; same engine with internal scope.
* **Frontend** (`frontend/`): Takshashila-branded static site (logo, maroon/gold),
  answer + verified citation cards, loading/error/empty states, staff-token dialog,
  health indicator, `?q=` share links, CSP, escaping, responsive; `build.py`.
* **Streamlit**: admin tabs gated by `ADMIN_PASSWORD`; HTML escaping; generic errors.
* **Docker**: CPU torch, baked model, offline HF, non-root, `$PORT`, HEALTHCHECK;
  `.dockerignore`; `railway.json`; `Dockerfile.mattermost` kept as an alias.
* **Workflows**: `ci.yml` (lint, tests, frontend build, docker build),
  `deploy-pages.yml`, `kb-refresh.yml`.
* **Repository hygiene**: internal data untracked; hardened `.gitignore`; dead code
  removed; `requirements-api.txt` / `requirements-dev.txt`; `pyproject.toml` (ruff, pytest).

## 3. Files changed

* **New**: `api/{__init__,main,schemas}.py`; `src/{url_utils,metadata,site_extract,people,
  supplementary,citation_format,refresh,kb_bundle,kb_sync}.py`; `scripts/{refresh_kb,
  refresh_gate,kb_release,audit_website,migrate_legacy_kb,benchmark,e2e_queries}.py`;
  `frontend/{index.html,styles.css,app.js,config.js,build.py,assets/*}`;
  `.github/workflows/{ci,deploy-pages,kb-refresh}.yml`; `Dockerfile`, `.dockerignore`,
  `railway.json`, `pyproject.toml`, `requirements-api.txt`, `requirements-dev.txt`;
  tests `conftest.py`, `test_{url_and_metadata,site_extract,crawler,retrieval,
  citation_verification,refresh,api,security_and_delivery,integration_real_index}.py`;
  docs `AUDIT_REPORT, IMPLEMENTATION_REPORT, ARCHITECTURE, DEPLOYMENT, CRAWLER,
  CRAWL_AUDIT, OPERATIONS, TESTING, TEST_REPORT, TROUBLESHOOTING`.
* **Rewritten/modified**: `scripts/{crawl_engine,validate_kb,update_knowledge_base,
  build_index,scheduler}.py`; `src/{config,vector_store,retriever,rag_pipeline,citations,
  chunker,incremental_index,crawl_state,embedding_cache,embeddings,extractors,
  groq_client,utils}.py`; `integrations/{mattermost_bot,mattermost_api,formatting}.py`;
  `app.py`; `Dockerfile.mattermost`; `requirements.txt`; `env.example`; `.gitignore`;
  `README.md`, `README_MATTERMOST_BOT.md`, `SETUP_KB_UPDATE.md`, `RAG_AUDIT_AND_UPGRADE.md`;
  tests `test_chunking.py` (rewritten), `test_answer_length.py` (prompt text).
* **Removed**: root `main.py`, `llm_handler.py`, `document_processor.py`, `config.py`,
  `utils.py`, `vector_store.py`; `src/scraper.py`, `src/ui_components.py`;
  `scripts/test_incremental_pipeline.py`, `scripts/test_metadata_qa.py`.
* **Untracked (kept on disk)**: `commit_kb_clean_crawled/`, `data/processed/`,
  `data/index/`, `data/knowledge_base/takshashila_{commit_kb,website}*`.

## 4. Tests run and results

See TEST_REPORT.md for the full record. Summary: 181 hermetic tests pass (3 opt-in
real-index tests skipped by default and run separately), ruff clean, Docker image
builds and passes a container smoke test, frontend builds, Streamlit renders without
exceptions, real-KB end-to-end questions pass, live incremental refreshes succeed.

## 5. Remaining blockers (need credentials, accounts or owner decisions)

1. **Git history** still contains internal Commit KB content (AUDIT S1) — purge or make
   the repo private (owner decision; affects GitHub Pages plan).
2. **Rotate** the Hugging Face token (S11) and consider rotating the Commit KB password.
3. **Railway**: project creation, variables and domain need your account (DEPLOYMENT §3).
4. **GitHub**: secrets/variables, Pages source, Actions write permission, pushing the
   branch and merging (DEPLOYMENT §1).
5. **Mattermost**: slash command Request URL + bot account on your server (DEPLOYMENT §5).
6. Not testable locally: GitHub Actions execution, Railway runtime, real Mattermost
   posting (bot token delivery). Everything else was exercised locally.
7. Local `.env`: `WEBSITE_MAX_PAGES=500`, `WEBSITE_MAX_DEPTH=4` cap local crawls at half
   the site — raise to 5000 / 8 (production values are set in the workflow).

## 6–12. Deployment, environment, Pages, Railway, Mattermost, KB commands

Exact steps: **DEPLOYMENT.md**. All variables: **env.example**. Operations and KB
commands: **OPERATIONS.md**. Short version:

```bash
# one-time
python -m src.kb_bundle genkey                                  # → KB_BUNDLE_KEY (GitHub secret + Railway)
git push -u origin production-hardening && gh pr create         # review, merge to main
gh secret set KB_BUNDLE_KEY; gh secret set COMMIT_KB_USERNAME; gh secret set COMMIT_KB_PASSWORD
gh variable set API_BASE_URL --body https://<service>.up.railway.app
gh workflow run kb-refresh.yml -f force=true                    # first KB release (or publish local build)
gh workflow run deploy-pages.yml

# daily (automatic, 06:00 IST) — manual equivalents
python scripts/refresh_kb.py            # incremental refresh (local)
gh workflow run kb-refresh.yml -f force=true
```
