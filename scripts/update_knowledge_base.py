#!/usr/bin/env python3
"""
update_knowledge_base.py — Backward-compatible entry point for KB updates.

All work is delegated to the single refresh implementation in src/refresh.py
(staging release → crawl → merge → incremental re-embed → validate → atomic
promotion). Prefer ``python scripts/refresh_kb.py``; this wrapper keeps the old
flags and the ``run()`` signature used by the Streamlit admin tab.

    python scripts/update_knowledge_base.py [--website-only|--commit-kb-only] [--full] [--no-index]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.refresh import run_refresh      # noqa: E402


def run(website: bool = True, commit_kb: bool = True, incremental: bool = True,
        do_index: bool = True, max_pages=None, max_depth=None, progress_cb=None) -> dict:
    """Run a refresh and return a summary in the legacy shape (merge/index/validation)."""
    report = run_refresh(website=website, commit_kb=commit_kb, full=not incremental,
                         dry_run=not do_index, max_pages=max_pages,
                         progress_cb=progress_cb or (lambda m: print(m, flush=True)))
    v = report.get("validation") or {}
    report["validation"] = {**v, "ok": v.get("status") == "passed",
                            "errors": v.get("errors", []), "warnings": v.get("warnings", [])}
    report.setdefault("merge", {"added": 0, "updated": 0, "removed": 0, "total": 0})
    if report["status"] == "failed":
        raise RuntimeError("; ".join(report.get("errors", [])) or "refresh failed")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Incrementally update the Takshashila knowledge base.")
    ap.add_argument("--website-only", action="store_true")
    ap.add_argument("--commit-kb-only", action="store_true")
    ap.add_argument("--full", "--rescrape-all", dest="full", action="store_true")
    ap.add_argument("--no-index", action="store_true", help="Detect changes only (dry run).")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--max-depth", type=int, default=None)
    args = ap.parse_args()
    try:
        run(website=not args.commit_kb_only, commit_kb=not args.website_only,
            incremental=not args.full, do_index=not args.no_index, max_pages=args.max_pages)
        return 0
    except Exception as exc:
        print(f"❌ Failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
