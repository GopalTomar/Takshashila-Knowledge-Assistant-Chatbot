#!/usr/bin/env python3
"""
migrate_legacy_kb.py — Build the first clean, versioned KB release.

The legacy flat KB (data/processed + data/index) was produced by an older
scraper: 618 documents carried polluted author fields, dates were in mixed
formats, op-eds were missing entirely and absolute local paths were stored.
Rather than merge new documents into that (URL de-duplication could keep a
polluted legacy copy), this builds a fresh release:

  1. full crawl of the website and the Commit KB into a NEW release directory,
     with every legacy document URL added as an extra seed (so nothing that was
     known before is skipped if it still exists);
  2. index build, validation and smoke tests (same code as the daily refresh);
  3. a reconciliation report — data/reports/legacy_reconciliation.json — listing
     every legacy document that is NOT in the new release and why (404, redirect,
     not linked any more, navigation page, too short…), so nothing disappears
     silently;
  4. promotion (data/releases/CURRENT) only if validation passes.

The legacy files are left untouched on disk.

    python scripts/migrate_legacy_kb.py [--no-commit-kb] [--no-promote] [--max-pages N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config                                    # noqa: E402
from src.url_utils import canonicalize_url                # noqa: E402
from src.utils import load_jsonl                          # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-commit-kb", action="store_true")
    ap.add_argument("--no-promote", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--resume", default="", help="Existing release dir name: skip crawling, rebuild + validate")
    args = ap.parse_args(argv)

    legacy_docs = load_jsonl(config.DATA_DIR / "processed" / "documents.jsonl")
    print(f"Legacy documents: {len(legacy_docs)}", flush=True)
    legacy_urls = {}
    for d in legacy_docs:
        u = canonicalize_url(d.get("url") or d.get("original_url") or "")
        if u:
            legacy_urls[u] = d

    from scripts.crawl_engine import commit_kb_config, crawl_site, website_config
    from scripts.validate_kb import validate
    from src.incremental_index import merge_documents, rebuild_index
    from src.refresh import _summarise_source, _write_crawl_log, promote, smoke_test

    run_id = args.resume or (datetime.now(ZoneInfo(config.KB_REFRESH_TIMEZONE)).strftime("%Y%m%dT%H%M%S")
                             + "-migration")
    release = config.RELEASES_DIR / run_id
    release.mkdir(parents=True, exist_ok=True)
    config.use_kb_root(release)
    t0 = time.time()
    report = {"run_id": run_id, "release": str(release), "sources": {}}

    all_docs = []
    sites = [("website", website_config(max_pages=args.max_pages))]
    if not args.no_commit_kb and config.COMMIT_KB_USERNAME:
        sites.append(("commit_kb", commit_kb_config(max_pages=args.max_pages)))
    if args.resume:
        sites = []
        for name in ("website", "commit_kb"):
            f = config.REPORTS_DIR / "crawl" / f"{name}_latest.json"
            if f.exists():
                for u, rec in json.loads(f.read_text(encoding="utf-8"))["urls"].items():
                    if u in legacy_urls:
                        legacy_urls[u]["_crawl"] = {k: rec.get(k) for k in ("status", "outcome", "final_url", "error")}
    for name, site in sites:
        site.extra_seeds = [u for u, d in legacy_urls.items()
                            if (d.get("source") == name or (name == "website" and d.get("source") == "pdf"))]
        print(f"── {name}: crawling with {len(site.extra_seeds)} legacy seeds", flush=True)
        res = crawl_site(site, incremental=True, progress_cb=lambda m: print(m, flush=True))
        report["sources"][name] = _summarise_source(res)
        _write_crawl_log(name, res, run_id)
        if res.status == "aborted":
            print(f"!! {name} aborted: {res.error}", flush=True)
            continue
        all_docs += res.docs
        for u, rec in res.log.items():
            if u in legacy_urls:
                legacy_urls[u]["_crawl"] = {k: rec.get(k) for k in ("status", "outcome", "final_url", "error")}

    merge = merge_documents(all_docs, removed_ids=[]) if not args.resume else {"resumed": run_id}
    report["merge"] = merge
    idx = rebuild_index(progress_cb=lambda m: print(m, flush=True), use_cache=True, version=run_id)
    report["index"] = idx
    v = validate()
    smoke = smoke_test()
    report["validation"] = {"ok": v["ok"] and not smoke, "errors": v["errors"] + smoke,
                            "warnings": v["warnings"], "counts": v["counts"]}

    # ── Reconciliation ─────────────────────────────────────────────────────────
    new_docs = load_jsonl(config.DOCUMENTS_FILE)
    new_urls = {canonicalize_url(d.get("url", "")) for d in new_docs}
    new_parents = {canonicalize_url(d.get("original_url", "")) for d in new_docs}
    missing = []
    for u, d in legacy_urls.items():
        if u in new_urls:
            continue
        c = d.get("_crawl") or {}
        final = canonicalize_url(c.get("final_url") or "") if c.get("final_url") else ""
        reason = c.get("outcome") or "not reached by crawl"
        if final and final != u and final in new_urls:
            reason = f"redirected → {final} (present in new KB)"
        elif u in new_parents:
            reason = "now represented by its parent page document"
        missing.append({"url": u, "legacy_document_id": d.get("document_id"), "title": d.get("title", "")[:150],
                        "source": d.get("source"), "http_status": c.get("status"), "reason": reason})
    report["legacy_reconciliation"] = {
        "legacy_documents": len(legacy_docs), "legacy_unique_urls": len(legacy_urls),
        "new_documents": len(new_docs), "legacy_not_in_new": len(missing),
        "reasons": dict(Counter(m["reason"].split(" →")[0] for m in missing)),
        "items": missing,
    }
    report["duration_seconds"] = round(time.time() - t0, 1)
    out = config.REPORTS_DIR / "legacy_reconciliation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    public = dict(report)
    out.write_text(json.dumps(public, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("merge", "validation")}, indent=2, default=str)[:3000])
    print("reconciliation:", json.dumps(report["legacy_reconciliation"]["reasons"]))

    if report["validation"]["ok"] and not args.no_promote:
        promote(release)
        print(f"✓ Promoted {release.name}")
        return 0
    print("✗ Not promoted (validation failed or --no-promote). Legacy KB remains active.")
    return 1 if not report["validation"]["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
