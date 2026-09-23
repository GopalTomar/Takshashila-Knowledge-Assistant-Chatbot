# Crawl audit — takshashila.org.in + Commit KB (2026-09-23)

Full crawl performed by the new engine on 2026-09-23 (IST) with every legacy
document URL added as an extra seed. Machine-readable outputs:
`data/reports/website_audit.json|csv`, `broken_links.csv`, `redirects.csv`,
`orphan_pages.csv`, `crawl_summary.json` (public website data, committed), and
`data/reports/crawl/*_latest.json` (per-URL logs; Commit KB log is internal, not committed).

## 1. Coverage

| | Legacy KB (baseline) | New KB |
|---|---|---|
| Website HTML documents | ~860 (incl. duplicates) | **1,002** pages (452 publications, 302 blogs, 95 profiles, 46 trackers, 22 courses, 18 events, 17 books, 17 research areas, 11 programmes, 11 about pages, 4 careers, 3 podcasts, …) |
| Website PDFs | 449 (`pdf` source, some duplicated) | **379** unique PDFs (from 401 PDF URLs checked) |
| Op-eds / media articles | **0** | **389** (from `/pages/news/`) |
| Commit KB pages | 58 | **62** (15 decisions, 29 playbook, 7 insights, 5 ideas, 3 notes, 3 index) |
| Curated internal files | 0 (holiday list not indexed) | **15** holiday documents |
| **Total** | 1,369 | **1,847** |

Sitemap: 1,009 URLs; crawl reached 1,278 page URLs + 401 PDF URLs; 264 pages were
reachable only via links (not in the sitemap), 1 only via the sitemap.

## 2. Metadata quality

| Field | Legacy | New |
|---|---|---|
| Polluted author values | 618 documents | **0** |
| Publications with author / date / categories | — | 440 / 452 / 447 of 452 |
| Blogs with author / date / categories | — | 297 / 302 / 301 of 302 |
| Op-eds with author / date / outlet | — | 388 / 389 / 389 |
| PDFs with author / date | — | 367 / 378 of 379 |
| Documents with author profile URLs | 0 | 1,162 |
| Team profiles with research areas / listed works | — | 32 / 992 works across 95 profiles |
| Dates in ISO format | 1,130 of 1,369 | all |
| Absolute local paths | 102 | 0 |
| Duplicate URLs | 4 | 0 |

The 3 publications without authors (`a-space-doctrine-for-india`,
`a-national-security-doctrine-for-india`, `beyond-the-himalayas`) have no AUTHOR row
on the live page — left empty rather than guessed.

## 3. Broken links and site issues (fix on the website)

* **291** internal URLs return 404; **239** of them are linked from live pages
  (567 page→link pairs in `broken_links.csv`); 47 were legacy-KB URLs that no longer exist.
* **169** broken links point to deleted team profiles still used as author links, e.g.
  `/content/team/west-asia-desk.html` (42 links), `anirudh-kanisetti.html` (21),
  `shrikrishna-upadhyaya.html` (14), `harshit-kukreja.html` (13), `suyash-desai.html` (10),
  `pavan-srinath.html` (10). The assistant keeps these authors as names but does not
  link their dead profile pages.
* 32 links to missing `/content/publications/…` pages and PDFs (e.g.
  `assets/a-pathway-to-ai-governance.pdf`); 4 links to an old `/research/…` URL scheme.
* Malformed links: an author link `https://Shyam Venkatesan`; an email written as a
  relative URL (`/content/team/bhumika@takshashila.org.in`); a `/pages/news/` entry
  without `https://` (`www.moneycontrol.com/…`) — handled by the extractor.
* **Wrong body under a title (12 duplicate-content groups)**, e.g.
  `20240110-the-quad-WIOR.html` and `20231206-israel-hamas-war-china-diplomacy.html`
  serve identical text; `content/blogs/trumps-climate-comeback.html` is titled
  "India must set up a Cabinet Committee on Science & Technology". 22 duplicate-title
  groups include genuine duplicates (`20241217-survey-of-military-ai-technologies.html`
  vs `20241217-A-Survey-of-Military-AI-Technologies.html`).
* 1 orphan page (in the sitemap, linked from nowhere):
  `/content/blogs/assets/2026-04-14-Oil Shock.html`.
* Raw spaces and `’` in 37 sitemap URLs (handled by canonicalisation).
* No timeouts, DNS failures, redirect loops or 5xx during the crawl.

### External links (fresh audit crawl with `--check-external`, 3,582 outbound links)

* **104 distinct external URLs are genuinely dead (404/410)** — e.g. 9 `doi.org`
  links, plus theguardian.com, whitehouse.gov, armscontrol.org, worldbank.org pages
  cited in publications. Listed with their source pages in `broken_links.csv`
  (`scope=external`).
* 728 × 403, 59 × 401, 58 × 999 (LinkedIn), 13 × 429 are mostly sites refusing
  automated requests or paywalls — **not** evidence the page is gone; review manually.
* 115 connection errors (TLS/refused), 25 × 5xx — likely transient; re-check before acting.

## 4. Commit KB ingestion report

| Metric | Value |
|---|---|
| Authentication | succeeded (HTTP basic) |
| Discovered (sitemap + links + legacy seeds) | 62 |
| Fetched successfully | 62 |
| Failed | 0 |
| Duplicates | 0 |
| Skipped / rejected | 0 |
| Retained | 62 (legacy KB had 58; 4 new pages since the last crawl) |

The legacy `commit_kb_clean_crawled/` export (with `.bak_20260617_151000` copies)
and `data/knowledge_base/takshashila_commit_kb*.json*` are superseded by the crawl
state + release documents and are no longer tracked in git.

## 5. Legacy reconciliation

`data/reports/legacy_reconciliation.json` (internal, not committed) lists every legacy
document absent from the new KB with the reason: 404 on the live site, redirected to a
URL present in the new KB, represented by its parent page, or a navigation page. No
legacy document was dropped without a recorded reason.

## 6. Re-running

```bash
python scripts/audit_website.py                    # fresh audit crawl (KB untouched)
python scripts/audit_website.py --from-last-crawl  # from the latest refresh log
python scripts/audit_website.py --check-external   # + outbound external links
```
The daily refresh writes the crawl log every day, so these reports stay current.
