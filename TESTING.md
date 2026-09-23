# Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                 # hermetic suite: no network, no LLM, no model download (~10 s)
ruff check .              # error-level lint (syntax, undefined names, unused imports)
RUN_INTEGRATION=1 pytest tests/test_integration_real_index.py -q   # real index + model
```

CI (`.github/workflows/ci.yml`) runs lint, the suite, a frontend build and a Docker build.

## How the suite stays hermetic

`tests/conftest.py` provides:

* `fake_embeddings` — a deterministic hashing embedder patched into `src.embeddings`;
  FAISS then behaves like lexical cosine, so ranking logic (RRF, filters, boosts,
  diversity) is exercised for real;
* `kb_root` — every test gets its own KB root / releases / reports dirs;
* `tiny_kb` — a realistic KB (publication, blog, team profile, op-ed, Commit KB note,
  holiday) built through the **real** pipeline (normalise → chunk → embed → FAISS →
  people graph → manifest).

Crawler and refresh tests use an in-memory fake website (`tests/test_crawler.py::FakeSite`)
with ETags, redirects, 404s, 503s, timeouts, robots.txt, a sitemap, a PDF (generated with
PyMuPDF) and an op-ed listing page.

## Coverage map

| Area | Tests |
|---|---|
| URL canonicalisation, content types, dates, author cleaning, schema (no fabrication) | `test_url_and_metadata.py` |
| Quarto extraction: publication details table, body, team profile relationships, listings | `test_site_extract.py` |
| Crawler: discovery, robots, sitemap lastmod, PDFs, op-eds, incremental 304/hash, change detection, removal confirmations, temporary failures, retries, redirects, auth abort, page cap | `test_crawler.py` |
| Chunking: sections, metadata propagation, stable IDs, OCR garbage, PDF pages, dedup | `test_chunking.py` |
| Retrieval: BM25 tokens, access scope, filters on both retrievers, author questions, title boost, diversity, determinism | `test_retrieval.py` |
| Citations: grouped/bare forms, fabricated numbers, wrong citations, mixed support, uncited attribution, hallucination refusal, citation records/excerpts | `test_citation_verification.py`, `test_citation_integrity.py` |
| RAG answer length/prompt | `test_answer_length.py`, `test_source_quality.py`, `test_mojibake.py` |
| Daily refresh automation: first build + promotion, change → only changed chunks embedded → retrievable, no-change → no embedding, crawl failure keeps production, validation failure not promoted, degraded after 3 failures, dry run, IST gate, next run | `test_refresh.py` |
| API: health, verified answers, public vs staff scope, invalid token, structured 422, insufficient evidence, search mode, LLM failure (no leak), rate limit, not-ready 503, sources/people/stats scope, Mattermost mounted | `test_api.py` |
| Mattermost: command parsing, routing, group delivery, progress indicator, signed callbacks, dialog state binding, slash token | `test_command_parser.py`, `test_group_delivery.py`, `test_progress_indicator.py`, `test_security_and_delivery.py` |
| Encrypted bundle (roundtrip, wrong key, tamper, truncation, checksum, tar-slip), frontend build (URL injection, https only), secrets scan, ignore files, pickle off | `test_security_and_delivery.py` |

Results of the last run are recorded in [TEST_REPORT.md](TEST_REPORT.md).
