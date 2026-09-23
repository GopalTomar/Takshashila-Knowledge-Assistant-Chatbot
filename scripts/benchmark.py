#!/usr/bin/env python3
"""
benchmark.py — Measure startup and query performance of the active KB.

    python scripts/benchmark.py [--llm] [--out data/reports/performance.json]

Measures: embedding-model load, FAISS+metadata load, BM25 build, first and
subsequent retrieval latency, query embedding time, LLM latency (with --llm, uses
GROQ_API_KEY), end-to-end answer latency, and process memory (RSS).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

QUERIES = [
    "What is Takshashila Institution?",
    "What are Takshashila's research areas?",
    "What has Takshashila published about geospatial technology?",
    "What op-eds has Pranay Kotasthane written?",
    "Tell me about the Geospatial Research programme",
    "What internal decisions has Takshashila recorded?",
]


def rss_mb() -> float:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1e6, 1)
    except Exception:
        return -1.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--out", default=str(config.REPORTS_DIR / "performance.json"))
    args = ap.parse_args(argv)
    r = {"kb_root": str(config.KB_ROOT), "rss_start_mb": rss_mb()}

    t = time.perf_counter()
    from src import embeddings, vector_store
    r["import_seconds"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    embeddings._get_model()
    r["model_load_seconds"] = round(time.perf_counter() - t, 2)

    import faiss
    t = time.perf_counter()
    idx = faiss.read_index(str(config.FAISS_INDEX))
    r["faiss_read_seconds"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    meta = json.loads(config.METADATA_JSON.read_text(encoding="utf-8"))
    r["metadata_load_seconds"] = round(time.perf_counter() - t, 2)
    r["vectors"], r["documents"] = idx.ntotal, len({m.get("document_id") for m in meta})
    del idx, meta

    t = time.perf_counter()
    st = vector_store.load_index(force=True)
    r["full_state_load_seconds_incl_bm25"] = round(time.perf_counter() - t, 2)
    r["rss_after_load_mb"] = rss_mb()

    from src.retriever import retrieve
    t = time.perf_counter()
    embeddings.embed_query("warm up")
    r["query_embedding_seconds"] = round(time.perf_counter() - t, 3)

    lat = []
    for i, q in enumerate(QUERIES):
        t = time.perf_counter()
        retrieve(q, top_k=5, allowed_sources=config.INTERNAL_SOURCES, state=st)
        lat.append(time.perf_counter() - t)
    r["retrieval_first_seconds"] = round(lat[0], 3)
    r["retrieval_median_seconds"] = round(statistics.median(lat[1:]), 3)
    r["retrieval_max_seconds"] = round(max(lat), 3)

    if args.llm and config.GROQ_API_KEY:
        from src.rag_pipeline import answer
        gen, total = [], []
        for q in QUERIES[:3]:
            t = time.perf_counter()
            res = answer(q, allowed_sources=config.INTERNAL_SOURCES)
            total.append(time.perf_counter() - t)
            gen.append(res.get("generation_time") or 0)
        r["llm_generation_median_seconds"] = round(statistics.median(gen), 2)
        r["answer_end_to_end_median_seconds"] = round(statistics.median(total), 2)
    r["rss_end_mb"] = rss_mb()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(r, indent=2), encoding="utf-8")
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
