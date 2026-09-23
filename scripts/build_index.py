#!/usr/bin/env python3
"""
build_index.py — Rebuild the index from the active release's documents (no crawl).

By default this goes through the same staged pipeline as the daily refresh:
the rebuild happens in a new release directory, is validated and smoke-tested,
and only then promoted — the serving KB is never modified in place. Unchanged
chunks are served from the embedding cache, so rebuilds are fast.

    python scripts/build_index.py              # staged rebuild + validation + promotion
    python scripts/build_index.py --in-place   # rebuild the active KB root directly (dev only)
    python scripts/build_index.py --in-place --no-cache
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the FAISS/BM25 index from documents.")
    ap.add_argument("--in-place", action="store_true", help="Rebuild the active root directly.")
    ap.add_argument("--no-cache", action="store_true", help="(with --in-place) embed everything.")
    args = ap.parse_args()
    if args.in_place:
        from src.incremental_index import rebuild_index
        summary = rebuild_index(progress_cb=lambda m: print(m, flush=True), use_cache=not args.no_cache)
        print(f"Done: {summary}")
        return 0
    from src.refresh import run_refresh
    report = run_refresh(rebuild_only=True, progress_cb=lambda m: print(m, flush=True))
    print(f"Rebuild {report['status']} (promoted={report['promoted']}); errors: {report.get('errors')}")
    return 0 if report["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
