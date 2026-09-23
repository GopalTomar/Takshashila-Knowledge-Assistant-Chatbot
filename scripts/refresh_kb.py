#!/usr/bin/env python3
"""
refresh_kb.py — Manual / scheduled knowledge-base refresh (one implementation).

    python scripts/refresh_kb.py                 # == --all, incremental (the daily job)
    python scripts/refresh_kb.py --all
    python scripts/refresh_kb.py --website       # website only
    python scripts/refresh_kb.py --commit-kb     # Commit KB only
    python scripts/refresh_kb.py --full          # ignore conditional GETs / hashes; re-crawl all
    python scripts/refresh_kb.py --dry-run       # detect changes, change nothing
    python scripts/refresh_kb.py --validate-only # validate the active release
    python scripts/refresh_kb.py --rebuild-only  # re-chunk/re-index active docs (no crawl)
    python scripts/refresh_kb.py --audit-only    # website link/crawl audit (no KB change)
    python scripts/refresh_kb.py --no-promote    # build + validate but keep current release

Exit codes: 0 success/partial/dry-run, 1 failed (production release untouched), 2 invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config                                   # noqa: E402
from src.refresh import refresh_status, run_refresh      # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Refresh the Takshashila knowledge base.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="Website + Commit KB (default).")
    g.add_argument("--website", action="store_true", help="Website only.")
    g.add_argument("--commit-kb", action="store_true", help="Commit KB only.")
    ap.add_argument("--full", action="store_true", help="Full crawl (ignore incremental state).")
    ap.add_argument("--dry-run", action="store_true", help="Detect changes only; modify nothing.")
    ap.add_argument("--validate-only", action="store_true", help="Validate the active release and exit.")
    ap.add_argument("--rebuild-only", action="store_true", help="Rebuild index from current documents.")
    ap.add_argument("--audit-only", action="store_true", help="Run the website audit (reports only).")
    ap.add_argument("--no-promote", action="store_true", help="Do not promote the new release.")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--seed", action="append", default=[], metavar="URL",
                    help="Also crawl this website URL (repeatable), e.g. an unlinked PDF.")
    ap.add_argument("--status", action="store_true", help="Print refresh status and exit.")
    ap.add_argument("--json", action="store_true", help="Print the full JSON report.")
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(refresh_status(), indent=2))
        return 0
    if args.validate_only:
        from scripts.validate_kb import _print_report, validate
        rep = validate()
        _print_report(rep)
        return 0 if rep["ok"] else 1
    if args.audit_only:
        from scripts.audit_website import main as audit_main
        return audit_main([] if args.max_pages is None else ["--max-pages", str(args.max_pages)])

    website = not args.commit_kb
    commit_kb = not args.website
    report = run_refresh(website=website, commit_kb=commit_kb, full=args.full,
                         dry_run=args.dry_run, promote_release=not args.no_promote,
                         rebuild_only=args.rebuild_only, max_pages=args.max_pages,
                         extra_seeds=args.seed,
                         progress_cb=lambda m: print(m, flush=True))
    slim = {k: v for k, v in report.items() if k not in ("traceback",)}
    if args.json:
        print(json.dumps(slim, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"\nRefresh {report['run_id']}: {report['status'].upper()}  "
              f"(promoted={report['promoted']}, {report.get('duration_seconds')}s)")
        print("Totals:", json.dumps(report.get("totals", {})))
        for e in report.get("errors", []):
            print("  ERROR:", e)
        print(f"Report: {config.DAILY_REPORTS_DIR / 'latest.json'}")
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
