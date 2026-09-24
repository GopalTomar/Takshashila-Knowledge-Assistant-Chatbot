# Test report — 2026-09-23

Environment: Windows 11, Python 3.12.10, CPU-only torch, Docker 29.8, branch
`production-hardening`. LLM: Groq `openai/gpt-oss-120b` (from local `.env`).

## 1. Automated suite

| Check | Result |
|---|---|
| `pytest -q` (hermetic) | **197 passed**, 3 skipped (opt-in integration), ~10 s |
| `RUN_INTEGRATION=1 pytest tests/test_integration_real_index.py` | **3 passed** (real 33k-vector index + bge-small) |
| `ruff check .` | **clean** |
| Import of every module in `src/`, `api/`, `integrations/`, `scripts/` | **53/53** import |
| Baseline suite before changes | 82 passed, 4 failed (real-index tests), 1 file un-collectable (`test_chunking.py`); real-index tests took **29 min 29 s** |

Per-file counts: api 15 · chunking 8 · citation_integrity 11 · citation_verification 12 ·
command_parser 25 · crawler 11 · group_delivery 9 · mojibake 12 · progress_indicator 6 ·
refresh 11 · retrieval 14 · security_and_delivery 14 · site_extract 5 · source_quality 12 ·
url_and_metadata 10 · answer_length 7 · integration (opt-in) 3.

## 2. Daily-refresh automation (spec Phase 19A) — all verified

| Requirement | Evidence |
|---|---|
| Simulated refresh builds and promotes | `test_first_refresh_builds_and_promotes` |
| Change detected; only changed chunks re-embedded; retrieval returns new content | `test_change_is_detected_and_only_changed_chunks_embedded` |
| Unchanged documents not re-processed | `test_no_change_means_no_reembedding`; live run 1: 1,071 unchanged, 0 embedded |
| Crawl failure keeps the old index active | `test_crawl_failure_keeps_production` |
| Validation failure → not promoted | `test_validation_failure_is_not_promoted` |
| 3 consecutive failures → degraded, data untouched | `test_repeated_failures_mark_degraded` |
| Asia/Kolkata schedule, never UTC | `test_gate_uses_ist_not_utc`, `test_next_scheduled_run_in_ist` |
| Processing-code change re-indexes from cache | `test_pipeline_change_triggers_reindex_without_content_change` |
| Removal safety (unlinked-but-live kept; 404 removed after confirmations) | 3 crawler tests |

## 3. Live runs against the real sources

| Run | Result |
|---|---|
| Full migration crawl (website + Commit KB) | 1,002 pages, 379 PDFs, 389 op-eds, 62 Commit KB pages, 15 holidays → 1,847 docs / 33,391 chunks; validation + smoke **passed**; promoted |
| Incremental refresh #1 | SUCCESS in 7 min; all pages `304`; 0 re-embedded; promoted atomically |
| Incremental refresh #2 (after code changes) | SUCCESS; 398 PDFs re-checked (0 changed); pipeline change → **181 of 33,280 chunks** re-embedded; promoted. **Found bug:** 3 live-but-unlinked PDFs were retired → fixed (direct 404 check + known-PDF re-checks) |
| Incremental refresh #3 (`--seed` the 3 PDFs) | SUCCESS; 3 PDFs restored (76 chunks); nothing else touched |
| Staged `--rebuild-only` | SUCCESS; 16 chunks re-embedded (listing reclassification) |
| Commit KB auth | succeeded; 62/62 fetched, 0 failed |
| Final audit `refresh_kb.py --all --dry-run` | exit 0 in 8 min; website 1,009 pages all `304`, 401 PDFs re-checked, 0 failed, 0 removed; Commit KB 1 new + 2 changed detected; nothing promoted. 244 `404`s are broken links on the live site (bad author slugs, malformed hrefs, 5 speculative listing seeds), none a KB document |
| Final audit container test | image built; encrypted bundle synced from a private-network server, `/ready` 200 in ~12 s; CORS allow/deny, public vs staff scope, 401 bad token, 403 bad slash token / unsigned action, guest check fails closed without bot token |

## 4. End-to-end questions (real KB + real LLM, `scripts/e2e_queries.py`)

Final run on the final code: **11/11 passed**. (One intermediate run hit Groq **429 rate
limits** — organisation quota exhausted by repeated testing — on two questions; the
harness now reports upstream errors separately from refusals, and the API maps provider
rate limits to `503` + `Retry-After`.) Earlier runs found and drove fixes for: PDF
boilerplate flooding generic questions (Q2), full-width `【Source N】` citations, Commit KB
index pages outranking content, and mixed-source questions returning one source only (Q10).

| # | Question | Result |
|---|---|---|
| 1 | What is Takshashila Institution? | pass — about pages cited |
| 2 | What are Takshashila's research areas? | pass — "Our Research Areas" page (was a refusal before boilerplate removal + intent boost) |
| 3 | Published about geospatial technology | pass — publications, event, op-ed |
| 4 | Publications by Pranay Kotasthane | pass — specific publications + his profile's publication list |
| 5 | Blogs/op-eds by Anupam Manur | pass — 4–5 op-eds with outlet and date; states no blogs found in context |
| 6 | Geospatial Research programme | pass — research-area page |
| 7 | Exact title "European Rearmament…" | pass — correct paper, author, date |
| 8 | Commit KB only (an internal playbook question) | pass — Commit KB playbook cited, `access: internal` |
| 9 | Website only (Policy School courses) | pass |
| 10 | Commit KB + website (AI use / AI governance) | pass — internal playbook and public PDF cited and correctly distinguished |
| 11 | Insufficient evidence (fictional lunar colony) | pass — refused, no citations |

Every answer's citations were checked automatically: every `[Source N]` maps to a shown
citation, every URL is http(s) and exists in the KB, titles match KB records.

## 5. API, container, UI

| Check | Result |
|---|---|
| Local API with real KB + LLM | health ready; CORS allows only the configured origin (other origin 400); public scope never returns Commit KB; staff token unlocks it; `/mattermost/ask` help 200, bad token 403, forged callback 403 |
| KB distribution | encrypted bundle 134 MB; fresh API with empty data dir downloaded, verified, decrypted, activated in 58 s; tampered bundle rejected with old KB serving; newer bundle hot-swapped with in-flight state intact |
| Docker image | builds (2.5 GB, CPU torch + baked model); non-root uid 10001; honours `PORT`; HEALTHCHECK healthy; no `.env` in image; with real KB mounted: warm 13.6 s, answer 2.6 s |
| Streamlit (`AppTest`) | renders with no exceptions; admin tabs hidden; wrong password rejected; correct password shows Build & Update / Automation |
| Frontend | builds; desktop and 500 px layouts verified by headless Edge screenshots; build rejects non-https API URLs and secret-like strings |

## 6. Performance (laptop, CPU; `scripts/benchmark.py`)

| Metric | Value |
|---|---|
| FAISS read / metadata load | 0.08 s / 3.0 s |
| Full state incl. BM25 | 10.1 s (BM25 build ≈ 6–7 s) |
| Embedding model load | 35 s on this laptop with Hub lookups; offline in container (warm total 13.6 s) |
| Query embedding | 0.08 s |
| Retrieval latency | first 0.21 s, median 0.19 s, max 0.31 s |
| LLM generation (median) | 4.6 s |
| Answer end-to-end (median) | 4.9 s |
| Process memory (RSS) | ~1.2 GB |
| Full embedding of 33k chunks (CPU) | ~55 min, checkpointed every 2,048 chunks |

## 7. Not tested here (needs your accounts)

GitHub Actions execution, GitHub Pages hosting, Railway runtime, real Mattermost
message delivery via the bot token. Each has a local equivalent that was exercised
(workflow YAML parsed; gate/refresh/pack/restore scripts run locally; container run
locally; bot routes exercised through the production API without posting).

## Render Free migration (2026-09-24)

Container runs of the production image under Render Free limits
(`docker run --memory 512m --memory-swap 512m --cpus 0.1 -e PORT=10000`), with the
real 33,356-chunk encrypted bundle served from a private Docker network.

| Check | Result |
|---|---|
| Memory before changes (torch + sentence-transformers + rank_bm25) | ~1.0–1.1 GB resident → **OOM-killed** at 512 MB |
| Memory after changes | ~290 MB heap + ~190 MB file-backed (memory-mapped model weights / FAISS, reclaimable); no OOM |
| Cold start at 0.1 CPU (download → verify → decrypt → load → smoke) | **~4 min** to `/ready` 200 (port bound in < 1 min) |
| Answer latency at 0.1 CPU | 16–21 s (`answer`), ~10 s (`search`); ~2.5 s on a full CPU |
| `scripts/smoke_test.py` against the constrained container | **15/15 passed** |
| Frontend end-to-end (headless Chrome, `?q=` link, cross-origin) | page loads, "Service online · KB <version>", answer + 4 inline citations + source links; API down → "Service unreachable" / clean error |
| Daily swap (`KB_LOW_MEMORY=true`), new version published | 503 for ~40 s, then new version served; no OOM |
| Corrupt bundle (checksum mismatch) | rejected; previous KB kept serving (no downtime) |

Equivalence of the memory changes (no retrieval logic changed):

| Change | Proof |
|---|---|
| ONNX query embedder (same bge-small weights) | parity vs sentence-transformers: min cosine 0.9999999, max abs diff 1.8e-7 (checked again in every Docker build; build fails otherwise) |
| CompactBM25 | 210 real-KB queries: scores **bit-identical** to `rank_bm25.BM25Okapi` |
| Streaming, value-sharing metadata reader | result `==` `json.load` on the real 96 MB file |
| End-to-end retrieval (20 queries × public/staff scope) | **40/40** result lists identical between the old and new stack |

`pytest -q`: 211 passed, 3 skipped; `ruff check .`: clean; real-index tests 3/3.
