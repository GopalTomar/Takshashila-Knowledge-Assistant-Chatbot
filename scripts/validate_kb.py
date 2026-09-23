#!/usr/bin/env python3
"""
validate_kb.py — Health checks for a knowledge-base root (production or staging).

Severity:
  ERROR (critical) — blocks promotion of a new release: empty/missing index,
      index↔metadata mismatch, duplicate document IDs or URLs, documents without
      text, chunks missing citation metadata (title + URL/source file), orphan
      chunks, BM25 unbuildable, a source that disappeared or a large document
      drop versus the previous release (protects against a broken crawl wiping
      knowledge), excessive mojibake / OCR garbage.
  WARN — quality issues worth fixing (missing author/date, chunk size outliers…).
  INFO — coverage statistics.

    python scripts/validate_kb.py            # report, exit 1 on ERROR
    python scripts/validate_kb.py --json     # JSON to stdout
    python scripts/validate_kb.py --strict   # exit 1 on WARN too

``validate(baseline=...)`` takes the previous release's counts for regression checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config                                   # noqa: E402
from src.utils import (                                  # noqa: E402
    get_logger, get_text_quality_score, is_true_mojibake_present, load_jsonl,
)

logger = get_logger("validate_kb", config.SCRAPE_LOG)

_UNDERSIZE = max(20, config.CHUNK_MIN_LEN // 3)
_OVERSIZE = int(config.CHUNK_SIZE * 2.2)
MAX_DOC_DROP_RATIO = 0.10      # >10% fewer documents than the previous release = critical
MAX_MOJIBAKE_RATIO = 0.01      # >1% of chunks with repairable-but-unrepaired mojibake
MAX_GARBAGE_RATIO = 0.01       # >1% of chunks that are OCR garbage


def _index_info() -> Dict:
    try:
        import faiss
        if not config.FAISS_INDEX.exists():
            return {"ntotal": -1, "dim": 0}
        idx = faiss.read_index(str(config.FAISS_INDEX))
        return {"ntotal": int(idx.ntotal), "dim": int(idx.d)}
    except Exception as exc:
        logger.warning(f"Could not read FAISS index: {exc}")
        return {"ntotal": -1, "dim": 0, "error": str(exc)}


def summarize_counts() -> Dict:
    """Small per-source document counts of the active root (used as a baseline)."""
    from src.incremental_index import load_all_documents
    docs = load_all_documents() if config.DOCUMENTS_FILE.exists() else []
    return {"documents": len(docs), "by_source": dict(Counter(d.get("source", "?") for d in docs))}


def validate(baseline: Optional[Dict] = None, check_bm25: bool = True) -> dict:
    errors, warns, infos = [], [], []
    from src.incremental_index import load_all_documents
    docs = load_all_documents() if config.DOCUMENTS_FILE.exists() else []
    chunks = []
    if config.METADATA_JSON.exists():
        try:
            chunks = json.loads(config.METADATA_JSON.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"metadata.json unreadable: {exc}")
    if not chunks:
        chunks = load_jsonl(config.CHUNKS_FILE)

    # ── Documents ───────────────────────────────────────────────────────────────
    ids = Counter(d.get("document_id") for d in docs)
    urls = Counter((d.get("url") or "").strip() for d in docs if (d.get("url") or "").strip())
    missing_url = [d.get("document_id") for d in docs
                   if not (d.get("url") or "").strip() and d.get("source") in ("website", "commit_kb")]
    missing_title = sum(1 for d in docs if not (d.get("title") or "").strip()
                        or d.get("title", "").strip().lower() == "untitled")
    empty_text = [d.get("document_id") for d in docs if not (d.get("text") or "").strip()]
    no_author = sum(1 for d in docs if d.get("content_type") in ("publication", "blog", "op-ed")
                    and not (d.get("authors") or d.get("author")))
    no_date = sum(1 for d in docs if d.get("content_type") in ("publication", "blog", "op-ed")
                  and not (d.get("publication_date") or d.get("date")))
    by_source = Counter(d.get("source", "?") for d in docs)
    by_type = Counter(d.get("content_type", "?") for d in docs)

    if not docs:
        errors.append("No documents found (documents.jsonl empty or missing).")
    else:
        infos.append(f"{len(docs)} documents; by source {dict(by_source)}; by type {dict(by_type)}")
    dup_ids = [k for k, n in ids.items() if n > 1]
    dup_urls = [k for k, n in urls.items() if n > 1]
    if dup_ids:
        errors.append(f"{len(dup_ids)} duplicate document ID(s), e.g. {dup_ids[:3]}")
    if dup_urls:
        errors.append(f"{len(dup_urls)} duplicate document URL(s), e.g. {dup_urls[:3]}")
    if missing_url:
        errors.append(f"{len(missing_url)} crawled document(s) missing a URL, e.g. {missing_url[:3]}")
    if empty_text:
        errors.append(f"{len(empty_text)} document(s) have empty text, e.g. {empty_text[:3]}")
    if missing_title:
        warns.append(f"{missing_title} document(s) missing a meaningful title.")
    if no_author:
        warns.append(f"{no_author} publication/blog/op-ed document(s) without an author.")
    if no_date:
        warns.append(f"{no_date} publication/blog/op-ed document(s) without a date.")

    # ── Regression guard vs previous release ─────────────────────────────────────
    if baseline and baseline.get("documents"):
        prev_n = baseline["documents"]
        if len(docs) < prev_n * (1 - MAX_DOC_DROP_RATIO):
            errors.append(f"Document count dropped from {prev_n} to {len(docs)} "
                          f"(> {MAX_DOC_DROP_RATIO:.0%}) — refusing to promote.")
        for src, n in (baseline.get("by_source") or {}).items():
            if n >= 5 and by_source.get(src, 0) == 0:
                errors.append(f"Source '{src}' had {n} documents and now has none.")

    # ── Chunks ──────────────────────────────────────────────────────────────────
    doc_ids = set(ids)
    if chunks:
        chunk_ids = Counter(c.get("chunk_id") for c in chunks)
        dup_chunk_ids = sum(n - 1 for n in chunk_ids.values() if n > 1)
        orphans = sum(1 for c in chunks if c.get("document_id") not in doc_ids) if doc_ids else 0
        no_cite = sum(1 for c in chunks if not (c.get("title") or "").strip()
                      or not ((c.get("url") or "").strip() or c.get("source_file")))
        mojibake = sum(1 for c in chunks if is_true_mojibake_present(c.get("text", "")))
        garbage = sum(1 for c in chunks if get_text_quality_score(c.get("text", "")) < 0.5)
        undersized = sum(1 for c in chunks if len(c.get("text") or "") < _UNDERSIZE)
        oversized = sum(1 for c in chunks if len(c.get("text") or "") > _OVERSIZE)
        n = len(chunks)
        infos.append(f"{n} chunks; avg {sum(len(c.get('text') or '') for c in chunks)//max(1, n)} chars")
        if dup_chunk_ids:
            errors.append(f"{dup_chunk_ids} duplicate chunk ID(s).")
        if orphans:
            errors.append(f"{orphans} orphan chunk(s) whose document is not in documents.jsonl.")
        if no_cite:
            errors.append(f"{no_cite} chunk(s) lack citation metadata (title + URL/source file).")
        if mojibake > n * MAX_MOJIBAKE_RATIO:
            errors.append(f"{mojibake} chunk(s) still contain mojibake.")
        elif mojibake:
            warns.append(f"{mojibake} chunk(s) contain mojibake-like sequences.")
        if garbage > n * MAX_GARBAGE_RATIO:
            errors.append(f"{garbage} chunk(s) look like OCR garbage.")
        if oversized:
            warns.append(f"{oversized} oversized chunk(s) (> {_OVERSIZE} chars).")
        if undersized:
            warns.append(f"{undersized} undersized chunk(s) (< {_UNDERSIZE} chars).")
    else:
        errors.append("No chunks / index metadata found — run a build.")

    # ── Index ───────────────────────────────────────────────────────────────────
    info = _index_info()
    if info["ntotal"] < 0:
        errors.append("FAISS index missing or unreadable.")
    elif chunks and info["ntotal"] != len(chunks):
        errors.append(f"Index/metadata mismatch: {info['ntotal']} vectors vs {len(chunks)} chunks.")
    elif chunks:
        infos.append(f"FAISS index: {info['ntotal']} vectors, dim {info['dim']}.")
    if info.get("dim") and info["dim"] != config.EMBEDDING_DIM:
        errors.append(f"Index dimension {info['dim']} != embedding model dimension {config.EMBEDDING_DIM}.")

    if check_bm25 and chunks:
        try:
            from rank_bm25 import BM25Okapi
            from src.utils import chunk_search_text
            from src.vector_store import bm25_tokenize
            sample = chunks[: min(len(chunks), 2000)]
            BM25Okapi([bm25_tokenize(chunk_search_text(c)) or ["_"] for c in sample])
        except Exception as exc:
            errors.append(f"BM25 index could not be built: {exc}")

    return {
        "ok": not errors,
        "root": str(config.KB_ROOT),
        "counts": {"documents": len(docs), "chunks": len(chunks), "index_vectors": info["ntotal"],
                   "by_source": dict(by_source), "by_content_type": dict(by_type)},
        "errors": errors, "warnings": warns, "info": infos,
    }


def _print_report(report: dict) -> None:
    print(f"Knowledge base validation — {'PASS' if report['ok'] else 'FAIL'}  ({report['root']})")
    for label, key in (("ERROR", "errors"), ("WARN", "warnings"), ("INFO", "info")):
        for m in report[key]:
            print(f"  [{label}] {m}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the knowledge base.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--output", help="Also write the JSON report to this path.")
    args = ap.parse_args()
    report = validate()
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        public = {k: v for k, v in report.items() if k != "root"}      # no local paths in reports
        public["kb_version"] = Path(report["root"]).name
        Path(args.output).write_text(json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_report(report)
    if not report["ok"] or (args.strict and report["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
