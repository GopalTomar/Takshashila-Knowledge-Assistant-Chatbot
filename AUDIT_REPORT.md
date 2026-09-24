# Audit report — Takshashila Knowledge Assistant

> **Deployment update (2026-09-24):** the backend now runs on **Render Free** instead of Railway — see `DEPLOYMENT.md`. Railway references below are historical.


Audit of the repository at baseline tag `baseline-pre-audit` (commit `2675516`,
2026-09-23), before the `production-hardening` branch. Every finding below was
verified against the code or data; the **Resolution** column says what was done
(details in IMPLEMENTATION_REPORT.md).

## 1. Architecture at baseline

```
laptop (Windows Task Scheduler, weekly Tue 09:00)
  └─ scripts/update_knowledge_base.py ─► crawl_engine (website + Commit KB)
        ─► data/processed/documents.jsonl (merged IN PLACE)
        ─► rebuild_index (in place: faiss.index + metadata.pkl + metadata.json)
Streamlit app.py ─► src.rag_pipeline ─► src.retriever (FAISS + BM25, RRF) ─► Groq
Mattermost ─► integrations/mattermost_bot.py (FastAPI, localhost:8000 + tunnel) ─► same pipeline
KB data (incl. internal Commit KB) committed to the PUBLIC GitHub repo
no API for a web frontend, no GitHub Pages, no hosted backend, no CI
```

Dependency graph (baseline): `app.py`, `integrations/*` → `src.rag_pipeline` →
`src.retriever` → `src.vector_store` → `src.embeddings`; `scripts/update_knowledge_base.py`
→ `scripts/crawl_engine.py` → `src.crawl_state`, `src.extractors`, `src.utils`;
`src.incremental_index` → `src.chunker`, `src.embedding_cache`. Root-level
`main.py`, `llm_handler.py`, `document_processor.py`, `config.py`, `utils.py`,
`vector_store.py` and `src/scraper.py`, `src/ui_components.py` were not imported by
anything.

## 2. Strengths worth keeping

* Hybrid FAISS + BM25 retrieval with Reciprocal Rank Fusion and source priority.
* Evidence gate + explicit insufficient-evidence reply.
* Incremental crawl state with ETag/Last-Modified and content hashes; embedding
  cache keyed by chunk content hash.
* Careful mojibake repair that preserves valid accented text; OCR-garbage filtering.
* Mattermost bot: clean command parser, delivery layer separate from retrieval,
  good test coverage for routing/progress/grouping.
* A single shared RAG engine for Streamlit and Mattermost.

All of these were preserved.

## 3. Findings

Severity: **C** critical · **H** high · **M** medium · **L** low.

### Security

| # | Sev | Finding | Resolution |
|---|---|---|---|
| S1 | C | The GitHub repo is **public** and tracks internal Commit KB content: `commit_kb_clean_crawled/` (internal decisions and playbook), `data/knowledge_base/takshashila_commit_kb*.json*`, and `data/processed` + `data/index` which embed it. | Untracked + git-ignored; KB now ships only as an AES-256-GCM encrypted bundle. History rewrite prepared with git-filter-repo and verified (no KB content or secret values remain); applying it to the local repo and force-pushing is the owner's step (see §6). |
| S2 | C | `/mattermost/action`, `/mattermost/dialog`, `/mattermost/feedback` accepted unauthenticated JSON. Anyone with the URL could make the bot post internal KB answers into arbitrary channels/users, trigger exports, or delete all bot posts in a channel. | HMAC-signed button contexts and dialog state (channel-bound); post↔channel check before deletes; 403 on unsigned callbacks. Tests added. |
| S3 | H | `Dockerfile.mattermost` did `COPY . .` with no `.dockerignore` → `.env` (Groq key, Commit KB password, Mattermost tokens) baked into any built image. | New Dockerfile copies only code; `.dockerignore` excludes `.env`, data, git. |
| S4 | H | Index metadata loaded with `pickle.load` by default (code execution if the file is tampered with). | JSON only; pickle requires `ALLOW_PICKLE_METADATA=true`; stale `.pkl` removed on rebuild. |
| S5 | M | Streamlit source cards interpolated crawled title/text/URL into `unsafe_allow_html` markup unescaped (HTML/JS injection from crawled pages); exceptions shown to users verbatim. | All values escaped; only http(s) links; generic error text. |
| S6 | M | Build/re-scrape/automation tabs available to every Streamlit user. | Hidden unless `ADMIN_PASSWORD` is set and entered. |
| S7 | M | Voice-link HMAC fell back to a hard-coded key `"takshashila-voice-fallback-secret"`. | Removed; voice disabled without a real secret. |
| S8 | L | Slash token compared with `!=`. | `hmac.compare_digest`. |
| S9 | L | Absolute local paths (`D:\project_takshashila_rag\…`) stored in 102 documents. | Normalised to relative paths. |
| S10 | L | Groq client had no timeout (a hung call blocked a worker forever). | 45 s timeout, 2 retries; API-level 60 s request timeout. |
| S11 | info | `.env` has duplicated keys and one malformed line (`HF_TOKEN = …`, spaces around `=`); that value was printed to this audit session's tool output by a redaction regex that missed it. | Owner action: rotate the Hugging Face token; tidy `.env`. |

### Crawler / ingestion

| # | Sev | Finding | Resolution |
|---|---|---|---|
| C1 | H | `scripts/crawl_engine.py` used `re` without importing it; any JSON-LD with string `keywords` raised `NameError` inside a bare `except`, silently dropping JSON-LD metadata. | Rewritten; lint in CI catches undefined names. |
| C2 | H | Author extraction fell back to `[class*='author']` and text regexes → **618 of 1,369 documents had polluted authors** (e.g. `"Bharath Reddy\n\nExecutive Summary\n\nTop"`, `"Transformation\nVanshika Saraf"`). | Quarto-aware extractor reads the AUTHOR row (names + profile URLs); `clean_authors` repairs legacy values. |
| C3 | H | **No op-eds**: `/pages/news/` lists ~390 op-eds/media pieces by staff; none were in the KB. | Each entry ingested as an `op-ed` document (title, authors, outlet, date, topics, link). |
| C4 | H | Team profiles were treated as "low-value" navigation → "What are X's research areas?" could not be answered. | Profiles kept; role, bio, research areas, listed works extracted; people graph built. |
| C5 | H | Removal on first miss: a URL not seen in one run was deleted. A transient 404 wave or partial outage could delete large parts of the KB. | Removal needs complete crawl + failure ratio < 20% + 2 consecutive misses; entry-point failure aborts safely. |
| C6 | M | No URL canonicalisation: links containing `#` were never followed; `/x/` vs `/x/index.html`; raw vs percent-encoded paths → **37 pages had two identities**. | `canonicalize_url` (fragments, tracking params, index.html, one encoding). |
| C7 | M | Category guessed from URL keywords (e.g. `data` → "Cyber & Digital") — fabricated metadata. | Removed; categories read from the page. |
| C8 | M | PDFs processed only when the parent page changed; never removed; PDF URLs from the sitemap fetched as HTML pages. | PDFs checked every run (byte hash), tracked as children of their page, retired with it. |
| C9 | M | Redirects ignored (documents keyed by the requested URL). | Final URL used; chain recorded; old document retired. |
| C10 | M | Failures only logged as warnings — no per-URL record, retry count, discovery path; no broken-link/orphan reports. | Per-URL audit log + `scripts/audit_website.py` reports. |
| C11 | M | Per-thread `sleep` ≈ 8 req/s with 8 workers; library retries hid counts. | Global rate limiter (4 req/s default); explicit retries with backoff + jitter + Retry-After. |
| C12 | M | Local `.env` caps `WEBSITE_MAX_PAGES=500`, `WEBSITE_MAX_DEPTH=4`; the site has ~1,000 pages → half the site silently skipped. | Capped crawls now reported as `partial` (warning) with removals skipped; production sets 5000/8. Owner: update local `.env`. |
| C13 | M | Scheme-less links in listings (`www.moneycontrol.com/…`) resolved onto takshashila.org.in → broken citation URLs. | Fixed in the listing extractor; test added. |
| C14 | L | Sitemap `lastmod` unused; no content types beyond publication/blog. | `modified_date` from lastmod; 20+ content types from the real IA. |
| C15 | L | Curated `holiday_list_2026.jsonl` was never indexed (config claimed supplementary files were "folded in"). | `src/supplementary.py` loads it; 15 holiday documents indexed. |

### Retrieval

| # | Sev | Finding | Resolution |
|---|---|---|---|
| R1 | H | BM25 ignored source/category/author/year filters → a "Commit KB only" query could return website chunks via BM25. | Same filter function for FAISS and BM25; access scope enforced in both. |
| R2 | H | Chunks < 200 chars classified "low-value" and deferred behind everything else → short Commit KB notes and the tail chunk of every document could never be retrieved even when they were the top FAISS **and** BM25 hit (reproduced in a test). | Length rule reduced to fragments (< 40 chars); thin pages are rejected at crawl time instead. |
| R3 | M | Filtered FAISS search only scanned top_k×12 rows → filtered queries returned few/no results. | Filtered queries scan the (exact, flat) index fully. |
| R4 | M | BM25 tokens kept punctuation (`ai,` ≠ `ai`). | Punctuation-free tokenizer + stopwords. |
| R5 | M | Author filter checked only `author`, not `authors`; no awareness of people named in the question. | Filter checks both; people graph adds author-filtered candidate lists + boost. |
| R6 | M | BM25-only hits had cosine 0 → confidence/evidence gate understated. | True cosine computed for every returned chunk. |
| R7 | L | No exact-title or URL match boost; ties ordered arbitrarily. | Title ×1.6, URL ×2.0 boosts; deterministic tie-break. |

### Citations / grounding

| # | Sev | Finding | Resolution |
|---|---|---|---|
| X1 | H | Any answer containing a valid `[Source N]` number was marked grounded; whether the cited passage supported the sentence was never checked (wrong citations passed). | Claim-level support check per sentence and cited passage; unsupported citations removed; mostly-unsupported answers refused. |
| X2 | H | Uncited answers were attributed to the top retrieved chunk (`v["sources"] or used_sources[:1]`) regardless of support. | Attribution only to passages that overlap the answer; otherwise refused. |
| X3 | M | Grouped forms (`[Source 1, 3]`, `[Sources 1 and 2]`) were dropped. | Normalised to individual markers. |
| X4 | L | Out-of-range numbers removed silently. | Recorded as `invalid_citations`. |
| X5 | L | Citation fields differed per interface (Streamlit cards, Mattermost cards). | One `build_citations` record used by API, Streamlit data, Mattermost (type/author/date added). |

### Performance / reliability

| # | Sev | Finding | Resolution |
|---|---|---|---|
| P1 | H | Index built and swapped **in place**; readers could see a new `faiss.index` with old metadata; no validation gate. | Versioned releases + atomic pointer; immutable in-memory `KBState` swap. |
| P2 | M | Whole-corpus `encode` in one call; a crash/sleep lost all progress (observed during this audit). | Embedding in 2,048-chunk blocks, cache checkpointed after each. |
| P3 | M | The two real-index retrieval tests took **29 min 29 s** (model download + pickle + BM25). | Hermetic suite: 170+ tests in ~10 s; real-index tests opt-in. |
| P4 | L | Model load contacts Hugging Face even when cached. | Offline fallback; containers run `HF_HUB_OFFLINE=1`. |

### Deployment

| # | Sev | Finding | Resolution |
|---|---|---|---|
| D1 | C | Refresh depended on a laptop (Windows Task Scheduler, weekly). | GitHub Actions daily refresh gated at 06:00 Asia/Kolkata. |
| D2 | H | No API for a web frontend; no GitHub Pages; no hosted backend; bot documented on localhost + tunnel. | FastAPI `api/main.py` (+ bot routes), `frontend/`, Railway config, Pages workflow. |
| D3 | H | Old image: CUDA torch (multi-GB), build-essential, root user, fixed port 8000, no health check. | CPU torch, baked model, non-root, `$PORT`, HEALTHCHECK, `railway.json`. |
| D4 | M | Self-hosted scheduler silently fell back to machine-local time if the timezone failed to load. | Fails loudly; schedule always interpreted in `KB_REFRESH_TIMEZONE`. |

### Tests / code health

| # | Sev | Finding | Resolution |
|---|---|---|---|
| T1 | M | `tests/test_chunking.py` imported functions that no longer existed → the file never ran. | Rewritten. |
| T2 | M | No tests for crawler, API, refresh, security, bundles. | Added (see TEST_REPORT.md). |
| Q1 | L | Dead/broken modules (§1) and a duplicated `_delete_post` in the bot (second silently overrode the first). | Removed. |
| Q2 | L | Docs described aspirational behaviour (e.g. supplementary files, weekly schedule, `metadata.pkl`). | Docs rewritten to the implementation. |

## 4. Coverage gaps found on the live site (content, not code)

From the full crawl (see CRAWL_AUDIT.md): 239 internal URLs linked from live pages
return 404 (e.g. missing team profiles linked as authors, a mailto written as a
relative link), 12 groups of duplicate page content and 22 duplicate titles. These
are website issues to fix at the source; the assistant reports them daily.

## 5. Residual risks

* Answer quality still depends on the LLM following instructions; verification is
  lexical (token overlap), so a paraphrased but correct claim can occasionally lose
  its citation, and a claim that reuses the passage's words but distorts it is not
  caught. The refusal path errs on the side of not answering.
* GitHub-hosted cron can start late under load; the gate tolerates delays (runs once
  on the first poll after 06:00 IST).
* Public API is rate-limited per IP in memory (single instance); put Cloudflare or
  Railway's proxy limits in front if abused.

## 6. Owner actions required

1. **Apply the prepared history rewrite and force-push** (commands in the final
   pre-deployment summary). Removed from all history: `commit_kb_clean_crawled/`,
   `data/raw/`, `data/logs/`, `data/index/`, `data/processed/`,
   `data/knowledge_base/takshashila_commit_kb*`, `data/knowledge_base/takshashila_website*`,
   `models/`. Because the repository was public, treat that content as already
   exposed: anyone who cloned it, forks, and GitHub's cached views may retain it —
   ask GitHub Support to purge cached views after the force-push.
2. **Rotate** the Hugging Face token (S11) and the Commit KB password (its value
   appeared inside crawled content that was public).
