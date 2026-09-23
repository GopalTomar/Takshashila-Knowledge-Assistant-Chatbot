# Crawler

Implementation: `scripts/crawl_engine.py` (engine), `src/site_extract.py`
(extraction), `src/url_utils.py` (URL identity), `src/crawl_state.py` (state),
`src/metadata.py` (schema). One engine crawls both sources.

## Sources

| Source | Base | Auth | Robots | Notes |
|---|---|---|---|---|
| `website` | `https://takshashila.org.in/` | none | respected | Quarto site, ~1,000 pages + ~400 PDFs + ~390 op-ed listings |
| `commit_kb` | `COMMIT_KB_URL` | HTTP basic (env) | n/a | Quarto site, ~60 pages; entry-point 401/403 aborts safely |

## Discovery

1. `robots.txt` (also adds any `Sitemap:` lines it declares).
2. `sitemap.xml` / sitemap indexes, recursively (depth ≤ 5), keeping `<lastmod>`.
3. Seeds: base URL + listing pages (`WEBSITE_LISTING_URLS`, incl. `/pages/news/`, `/pages/team/`).
4. Every URL already in the crawl state (so nothing previously known is skipped).
5. BFS over internal links (depth ≤ `WEBSITE_MAX_DEPTH`, ≤ `WEBSITE_MAX_PAGES`).

Every URL is canonicalised: fragments removed, tracking params removed, query
sorted, `..`/`//` resolved, `/index.html` ≡ `/`, one percent-encoding.

## Site information architecture (from the live sitemap)

| Path | Content type | Count (Sep 2026) |
|---|---|---|
| `/content/publications/` | publication (+ PDFs in `assets/`) | 452 |
| `/content/blogs/` | blog | 302 |
| `/content/team/` | person (profile) | 95 |
| `/content/trackers/`, `/pages/trackers/` | tracker / newsletter bulletins | 47 |
| `/pages/policy-school/` | course | 22 |
| `/content/events/` | event | 19 |
| `/content/books/` | book | 17 |
| `/pages/research-areas/`, `/pages/ai` | research area (incl. Geospatial Research) | 17 |
| `/content/fellowships/`, `/pages/fellowships` | programme | 11 |
| `/pages/*.html` (about, mission, heritage…) | about | 11 |
| `/content/podcasts/` | podcast | 4 |
| `/content/careers/` | career | 4 |
| `/pages/news/` | listing of op-eds / media pieces → one `op-ed` document per entry | ~390 |

## Extraction (Quarto markup)

* Title: `#title-block-header h1`; subtitle/role: `.gcpp-hero__lede`.
* "Document Details" table: **AUTHOR** (names + profile URLs), **DATE**, **DOCUMENT**
  (series, e.g. "Takshashila Discussion Document 2025-06"), **VERSION**, **CATEGORIES**.
* Body: `main#quarto-document-content` block by block; headings kept as Markdown
  (`#`, `##`) so chunks carry a `heading_path`; scripts/nav/footer/listings removed.
* Profiles: role, biography, research areas, and the person's listed publications,
  blogs and "In the news" op-eds (with co-authors, outlet, date).
* PDFs: text per page (PyMuPDF) + embedded PDF metadata; title/authors/date from the
  linking page.
* Values are only read from the page — nothing is guessed (the old URL-keyword
  "category" guessing and text-regex author guessing are no longer used).

## Change detection

| Signal | Use |
|---|---|
| ETag / Last-Modified | conditional GET → `304` = no download |
| sitemap `lastmod` | stored as `modified_date`, recorded in state |
| raw PDF bytes hash | skip PDF extraction when identical |
| content hash (text + title/authors/date/categories) | **authoritative** new/modified/unchanged decision |
| op-ed entry hash (text + target URL) | per-entry change detection |

The site redeploys daily (ETags churn), so the content hash is what prevents
reprocessing; unchanged documents are never re-chunked or re-embedded.

**Extractor fingerprint.** Because unchanged pages are skipped, a fix to the
extraction code (`src/site_extract.py`, `src/metadata.py`, `src/url_utils.py`)
would never reach them. The crawl state records `extractor_version`; when the code's
fingerprint differs, the next scheduled run re-extracts every page once (a full
crawl), then returns to incremental. Unchanged chunks still come from the embedding
cache. The first run after this feature ships does one such pass (it also repairs
an op-ed whose scheme-less link was resolved against takshashila.org.in by an older
extractor).

## Failure handling

| Situation | Behaviour |
|---|---|
| timeout / connection error / 429 / 5xx | retried (`SCRAPE_MAX_RETRIES`) with exponential backoff + jitter (Retry-After honoured); if still failing the previous document is kept and marked `unavailable_since` |
| 404 / 410 | recorded (`gone`), document kept; removal only after `KB_REMOVAL_CONFIRMATIONS` consecutive complete crawls |
| page no longer linked or listed | same confirmation rule |
| redirect | chain recorded; document moves to the final URL; old document retired |
| base URL 401/403/unreachable | crawl of that source aborted — no documents change |
| page cap reached | status `partial`; removal detection skipped |
| failure ratio > `KB_MAX_FAILURE_RATIO` | removal detection skipped |

## Audit trail

Every URL gets a record in `CrawlResult.log` (written to
`data/reports/crawl/<source>_latest.json`): status, final URL, redirect chain,
error type, retries, fetched_at, elapsed, discovery methods, discovered_from,
inlinks, outlinks, title, text length, outcome. `scripts/audit_website.py` turns
this into the broken-link / redirect / orphan / QA reports.

## Commands

```bash
python scripts/refresh_kb.py --website --dry-run      # what would change (no writes)
python scripts/refresh_kb.py --website                # incremental website refresh
python scripts/refresh_kb.py --commit-kb              # Commit KB only
python scripts/refresh_kb.py --all --full             # full crawl
python scripts/audit_website.py                       # fresh audit crawl (KB untouched)
python scripts/audit_website.py --from-last-crawl --check-external
```
