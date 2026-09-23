#!/usr/bin/env python3
"""
audit_website.py — Broken links, redirects, orphans and crawl QA for the website.

    python scripts/audit_website.py                    # fresh audit crawl (KB untouched)
    python scripts/audit_website.py --from-last-crawl  # analyse the last refresh's crawl log
    python scripts/audit_website.py --check-external   # also check outbound external links

Outputs (data/reports/):
    website_audit.json   summary + every URL record
    website_audit.csv    one row per URL
    broken_links.csv     every (page → broken link) pair with status/error/retries
    redirects.csv        redirect chains, hop counts, loops
    orphan_pages.csv     pages in the sitemap that no crawled page links to
    crawl_summary.json   counts for every QA check

Detects 404/403/410/5xx, timeouts, DNS failures, malformed links, redirect
chains/loops, orphan / sitemap-only / links-only pages, pages with no or very
short text, missing titles, missing metadata (author/date on publications,
blogs), duplicate content and duplicate titles. Nothing is discarded silently:
every failure row carries URL, status, error, discovery path, timestamp, retries.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config                                   # noqa: E402
from src.metadata import classify_content_type           # noqa: E402
from src.utils import now_iso                            # noqa: E402

SHORT_TEXT = 300
BROKEN_TYPES = ("timeout", "dns", "connection", "redirect_loop", "malformed")


def _is_broken(rec: Dict) -> bool:
    st = rec.get("status")
    return (isinstance(st, int) and (st in (403, 404, 410) or st >= 500)) or \
        rec.get("error_type") in BROKEN_TYPES


def _write_csv(path: Path, rows: List[Dict], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
                        for k, v in r.items()})


def _check_external(urls: List[str], max_workers: int = 8) -> Dict[str, Dict]:
    import requests
    out: Dict[str, Dict] = {}
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT

    def check(u: str) -> None:
        rec = {"url": u, "status": None, "error": "", "checked_at": now_iso(), "retries": 0}
        for attempt in range(2):
            rec["retries"] = attempt
            try:
                r = s.head(u, allow_redirects=True, timeout=15)
                if r.status_code in (403, 405, 501):      # many sites reject HEAD
                    r = s.get(u, allow_redirects=True, timeout=20, stream=True)
                rec["status"], rec["final_url"] = r.status_code, r.url
                rec["error"] = ""
                break
            except requests.RequestException as exc:
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
                time.sleep(1.5)
        out[u] = rec

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(check, urls))
    return out


def analyse(log: Dict[str, Dict], external: Dict[str, Dict] = None) -> Dict:
    rows = []
    text_hashes = defaultdict(list)
    titles = defaultdict(list)
    for url, r in log.items():
        disc = r.get("discovery") or []
        kind = r.get("kind") or ("pdf" if url.lower().endswith(".pdf") else "page")
        ctype = classify_content_type(url, "website")
        row = {
            "url": url, "kind": kind, "content_type": ctype, "status": r.get("status"),
            "outcome": r.get("outcome"), "final_url": r.get("final_url") or "",
            "redirect_hops": len(r.get("redirect_chain") or []), "error_type": r.get("error_type", ""),
            "error": r.get("error", ""), "retries": r.get("retries", 0),
            "discovered_from": r.get("discovered_from", ""), "discovery": disc,
            "in_sitemap": "sitemap" in disc, "via_links": "links" in disc,
            "inlink_count": len(r.get("inlinks") or []), "depth": r.get("depth"),
            "title": r.get("title", ""), "text_len": r.get("text_len"),
            "has_author": r.get("has_author"), "has_date": r.get("has_date"),
            "fetched_at": r.get("fetched_at", ""), "outlink_count": len(r.get("outlinks") or []),
            "sitemap_lastmod": r.get("sitemap_lastmod", ""),
        }
        rows.append(row)
        if kind == "page" and r.get("status") == 200 and r.get("text_hash") and (r.get("text_len") or 0) > 0:
            text_hashes[r["text_hash"]].append(url)
        if kind == "page" and r.get("title"):
            titles[r["title"].strip().lower()].append(url)

    broken_targets = {r["url"]: r for r in rows if _is_broken(r)}
    broken_links = []
    for url, r in log.items():
        if url in broken_targets:
            b = broken_targets[url]
            for src in (r.get("inlinks") or [r.get("discovered_from") or "(sitemap/seed)"]):
                broken_links.append({"source_page": src, "link_url": url, "status": b["status"],
                                     "error_type": b["error_type"], "error": b["error"],
                                     "retries": b["retries"], "discovery": b["discovery"],
                                     "timestamp": b["fetched_at"], "scope": "internal"})
        for bad in r.get("malformed_links") or []:
            broken_links.append({"source_page": url, "link_url": bad, "status": "", "error_type": "malformed",
                                 "error": "malformed href", "retries": 0, "timestamp": r.get("fetched_at", ""),
                                 "scope": "internal"})
    if external:
        ext_sources = defaultdict(list)
        for url, r in log.items():
            for e in r.get("external_links") or []:
                ext_sources[e].append(url)
        for u, rec in external.items():
            st = rec.get("status")
            if rec.get("error") or (isinstance(st, int) and st >= 400):
                for src in ext_sources.get(u) or ["(document metadata)"]:
                    broken_links.append({"source_page": src, "link_url": u, "status": st,
                                         "error_type": "external", "error": rec.get("error", ""),
                                         "retries": rec.get("retries", 0), "timestamp": rec.get("checked_at"),
                                         "scope": "external"})

    redirects = [{"url": r["url"], "final_url": r["final_url"], "hops": r["redirect_hops"],
                  "chain": log[r["url"]].get("redirect_chain"),
                  "loop": r["error_type"] == "redirect_loop"}
                 for r in rows if r["redirect_hops"] or r["error_type"] == "redirect_loop"]
    orphans = [r for r in rows if r["in_sitemap"] and r["inlink_count"] == 0 and r["kind"] == "page"
               and urlsplit(r["url"]).path not in ("", "/")]
    ok_pages = [r for r in rows if r["kind"] == "page" and r["status"] == 200]
    article_types = ("publication", "blog")
    summary = {
        "generated_at": now_iso(),
        "urls_total": len(rows),
        "pages": sum(1 for r in rows if r["kind"] == "page"),
        "pdfs": sum(1 for r in rows if r["kind"] == "pdf"),
        "status_counts": dict(Counter(str(r["status"]) for r in rows)),
        "outcomes": dict(Counter(str(r["outcome"]) for r in rows)),
        "by_content_type": dict(Counter(r["content_type"] for r in ok_pages)),
        "broken_internal": len(broken_targets),
        "broken_link_rows": len(broken_links),
        "broken_by_status": dict(Counter(str(b["status"] or b["error_type"]) for b in broken_links)),
        "timeouts": sum(1 for r in rows if r["error_type"] == "timeout"),
        "dns_failures": sum(1 for r in rows if r["error_type"] == "dns"),
        "redirects": len(redirects),
        "redirect_loops": sum(1 for r in redirects if r["loop"]),
        "multi_hop_redirects": sum(1 for r in redirects if (r["hops"] or 0) > 1),
        "orphan_pages": len(orphans),
        "sitemap_only_pages": sum(1 for r in rows if r["in_sitemap"] and not r["via_links"] and r["kind"] == "page"),
        "links_only_pages": sum(1 for r in rows if r["via_links"] and not r["in_sitemap"] and r["kind"] == "page"),
        "no_text_pages": sum(1 for r in ok_pages if not r["text_len"]),
        "short_text_pages": sum(1 for r in ok_pages if r["text_len"] and r["text_len"] < SHORT_TEXT),
        "missing_title": sum(1 for r in ok_pages if not r["title"]),
        "articles_missing_author": sum(1 for r in ok_pages if r["content_type"] in article_types and not r["has_author"]),
        "articles_missing_date": sum(1 for r in ok_pages if r["content_type"] in article_types and not r["has_date"]),
        "duplicate_content_groups": [urls for urls in text_hashes.values() if len(urls) > 1],
        "duplicate_title_groups": [urls for t, urls in titles.items() if len(urls) > 1],
        "failed_fetches": [r for r in rows if r["outcome"] in ("failed", "error")],
        "external_links_checked": len(external or {}),
    }
    return {"summary": summary, "rows": rows, "broken_links": broken_links,
            "redirects": redirects, "orphans": orphans}


def write_reports(result: Dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    s = result["summary"]
    (out_dir / "website_audit.json").write_text(json.dumps(
        {"summary": s, "urls": result["rows"]}, ensure_ascii=False, indent=1), encoding="utf-8")
    compact = {k: (len(v) if isinstance(v, list) else v) for k, v in s.items()}
    compact["duplicate_content_examples"] = s["duplicate_content_groups"][:20]
    compact["duplicate_title_examples"] = s["duplicate_title_groups"][:20]
    (out_dir / "crawl_summary.json").write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(out_dir / "website_audit.csv", result["rows"], list(result["rows"][0].keys()) if result["rows"] else ["url"])
    _write_csv(out_dir / "broken_links.csv", result["broken_links"],
               ["source_page", "link_url", "scope", "status", "error_type", "error", "retries", "discovery", "timestamp"])
    _write_csv(out_dir / "redirects.csv", result["redirects"], ["url", "final_url", "hops", "loop", "chain"])
    _write_csv(out_dir / "orphan_pages.csv", result["orphans"],
               ["url", "content_type", "title", "status", "sitemap_lastmod", "discovery"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-last-crawl", action="store_true")
    ap.add_argument("--check-external", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--out-dir", default=str(config.REPORTS_DIR))
    args = ap.parse_args(argv)

    if args.from_last_crawl:
        src = config.REPORTS_DIR / "crawl" / "website_latest.json"
        if not src.exists():
            print(f"No crawl log at {src}; run a refresh or omit --from-last-crawl.", file=sys.stderr)
            return 1
        log = json.loads(src.read_text(encoding="utf-8"))["urls"]
    else:
        from scripts.crawl_engine import crawl_site, website_config
        res = crawl_site(website_config(max_pages=args.max_pages), audit_only=True,
                         progress_cb=lambda m: print(m, flush=True))
        log = res.log

    external = None
    if args.check_external:
        from src.incremental_index import load_all_documents
        ext = set()
        for d in load_all_documents():
            if d.get("source") != "website":
                continue
            ext.update(d.get("external_links") or [])
            if d.get("content_type") == "op-ed":
                ext.add(d.get("url"))
            if d.get("url") in log:
                log[d["url"]]["external_links"] = d.get("external_links") or []
        print(f"Checking {len(ext)} external links…", flush=True)
        external = _check_external(sorted(u for u in ext if u))

    result = analyse(log, external)
    write_reports(result, Path(args.out_dir))
    s = result["summary"]
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in s.items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
