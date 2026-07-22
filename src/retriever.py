"""
retriever.py — Hybrid BM25 + FAISS retrieval with source-priority ranking.

Pipeline:
  1. FAISS semantic search (raw cosine 'score' kept on every hit).
  2. BM25 lexical search (optional, improves keyword recall).
  3. Reciprocal Rank Fusion to combine the two rankings.
  4. Source-priority boost: Commit KB > Staff Handbook > everything else.
  5. Per-document dedup, then top-k.

Each returned chunk carries:
  - score      : best raw cosine similarity (used for the evidence gate)
  - rrf_score  : fused, priority-boosted ranking score (used for ordering)
"""

from typing import Dict, List, Optional

from src import config
from src.utils import clean_chunk_metadata, get_logger
from src import vector_store
from src.vector_store import search as faiss_search

logger = get_logger("retriever", config.SCRAPE_LOG)

_BM25 = None
_BM25_CHUNKS: List[Dict] = []
_BM25_SIG = None   # (id(metadata_list), len) — used to detect a rebuilt index


def ensure_bm25_ready():
    """
    Lazy-build a BM25 index over the SAME chunk list FAISS already loaded.

    We deliberately reuse ``vector_store.get_metadata()`` rather than re-reading
    chunks.jsonl, so the process keeps a single full copy of the chunk text in
    memory (instead of one for FAISS and another for BM25). The index is rebuilt
    only if the underlying metadata changes (e.g. after a rebuild), which is
    cheap to detect via the list's identity + length.
    """
    global _BM25, _BM25_CHUNKS, _BM25_SIG
    try:
        chunks = vector_store.get_metadata()
    except Exception as exc:
        logger.warning(f"BM25 setup skipped (index not loaded): {exc}")
        return

    sig = (id(chunks), len(chunks))
    if _BM25 is not None and _BM25_SIG == sig:
        return   # already built for this exact metadata list

    if not chunks:
        return
    try:
        from rank_bm25 import BM25Okapi
        from src.utils import chunk_search_text
        tokenised = [chunk_search_text(ch).lower().split() for ch in chunks]
        _BM25 = BM25Okapi(tokenised)
        _BM25_CHUNKS = chunks          # reference, not a copy
        _BM25_SIG = sig
        logger.info(f"BM25 index built over {len(chunks)} chunks (shared metadata)")
    except Exception as exc:
        logger.warning(f"BM25 setup failed (using FAISS-only): {exc}")


# Backwards-compatible alias for any external caller / older imports.
def _ensure_bm25():
    ensure_bm25_ready()


def bm25_search(query: str, top_k: int = 20) -> List[Dict]:
    """Return top BM25 results."""
    ensure_bm25_ready()
    if _BM25 is None or not _BM25_CHUNKS:
        return []
    scores = _BM25.get_scores(query.lower().split())
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    return [{**_BM25_CHUNKS[idx], "bm25_score": float(score)}
            for idx, score in ranked[:top_k] if score > 0]


def _cid(ch: Dict, fallback) -> str:
    return ch.get("chunk_id") or ch.get("chunk_hash") or str(fallback)


def retrieve(
    query: str,
    top_k: int = config.TOP_K,
    source: Optional[str] = None,
    category: Optional[str] = None,
    author: Optional[str] = None,
    year: Optional[str] = None,
    use_hybrid: bool = True,
    # legacy aliases
    source_type: Optional[str] = None,
) -> List[Dict]:
    """Hybrid retrieval with source-priority ranking and dedup."""
    if source is None and source_type is not None:
        source = source_type

    faiss_results = faiss_search(
        query, top_k=top_k * 3,
        source_filter=source,
        category_filter=category,
        author_filter=author,
        year_filter=year,
    )
    bm25_results = bm25_search(query, top_k=top_k * 3) if use_hybrid else []

    # ── Reciprocal Rank Fusion ─────────────────────────────────────────────
    k_rrf = 60
    fused: Dict[str, float] = {}
    chunk_map: Dict[str, Dict] = {}

    for rank, r in enumerate(faiss_results):
        cid = _cid(r, rank)
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k_rrf + rank + 1)
        chunk_map[cid] = r   # has cosine 'score'

    for rank, r in enumerate(bm25_results):
        cid = _cid(r, f"b{rank}")
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k_rrf + rank + 1)
        if cid not in chunk_map:
            chunk_map[cid] = r   # BM25-only: may have no cosine 'score'

    # ── Source-priority boost ───────────────────────────────────────────────
    for cid, base in fused.items():
        src = (chunk_map[cid].get("source") or chunk_map[cid].get("source_type") or "")
        tier = config.source_priority(src)
        boost = 1.0 + (tier - config.DEFAULT_SOURCE_PRIORITY) * config.SOURCE_PRIORITY_BOOST
        fused[cid] = base * boost

    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

    # ── Dedup + evidence-first selection ─────────────────────────────────────
    # At most 2 chunks per source document. Navigational / listing / landing
    # pages are held back (deferred) so they never push a real article out of
    # the top-k; if too few genuine-evidence chunks exist, we backfill from the
    # deferred pool so results are never empty.
    from src.utils import chunk_is_low_value

    doc_count: Dict[str, int] = {}
    taken: set = set()
    final: List[Dict] = []

    def _select(consider_low_value: bool) -> None:
        for cid, rrf_score in ranked:
            if len(final) >= top_k:
                return
            if cid in taken:
                continue
            ch = chunk_map[cid]
            doc_id = ch.get("document_id") or ch.get("doc_id") or cid
            if chunk_is_low_value(ch) != consider_low_value:
                continue          # pass 1 keeps evidence; pass 2 backfills nav/listing
            if doc_count.get(doc_id, 0) >= 2:
                continue
            doc_count[doc_id] = doc_count.get(doc_id, 0) + 1
            taken.add(cid)
            out = {**ch, "rrf_score": rrf_score}
            out.setdefault("score", float(ch.get("score", 0.0)))  # ensure a cosine score field
            final.append(clean_chunk_metadata(out))   # safety net if index predates cleaning

    _select(consider_low_value=False)     # genuine evidence first
    if not final:
        _select(consider_low_value=True)  # backfill only if NOTHING else exists

    return final


def best_cosine(chunks: List[Dict]) -> float:
    """Best raw cosine similarity among retrieved chunks (0 if none)."""
    return max((float(c.get("score", 0.0)) for c in chunks), default=0.0)


def confidence_level(chunks: List[Dict]) -> str:
    """Map the best cosine score to a confidence tier."""
    top = best_cosine(chunks)
    if top >= config.CONF_HIGH_THRESHOLD:
        return "high"
    if top >= config.CONF_MEDIUM_THRESHOLD:
        return "medium"
    if top >= config.MIN_SCORE_THRESHOLD:
        return "low"
    return "none"


def has_sufficient_evidence(chunks: List[Dict]) -> bool:
    """True only if the best cosine score clears the minimum threshold."""
    return best_cosine(chunks) >= config.MIN_SCORE_THRESHOLD