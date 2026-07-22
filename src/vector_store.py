"""
vector_store.py — FAISS index build, save, load, and search.

- Builds a cosine-similarity index (IndexFlatIP over normalized embeddings).
- Saves the FAISS index and the chunk metadata separately (metadata as JSON,
  with a .pkl mirror for backward compatibility).
- Supports full rebuild, loading an existing index, and incremental add.
- Deduplicates chunks by content hash before indexing.
- Emits clear logs for #documents and #chunks indexed.
"""

import json
import pickle
from typing import Dict, List, Optional, Tuple

import numpy as np

from src import config
from src.embeddings import embed_texts, embed_query
from src.utils import clean_chunk_metadata, get_logger, load_jsonl

logger = get_logger("vector_store", config.SCRAPE_LOG)

# JSON metadata sidecar (human-readable); .pkl kept for legacy loaders.
METADATA_JSON = config.INDEX_DIR / "metadata.json"


# ── Build ──────────────────────────────────────────────────────────────────────

def build_index(progress_cb=None, chunks: Optional[List[Dict]] = None) -> Tuple[int, int]:
    """
    Load chunks (from CHUNKS_FILE unless provided), embed, build a FAISS
    IndexFlatIP, and save index + metadata. Returns (num_chunks, embedding_dim).
    """
    import faiss

    if chunks is None:
        chunks = load_jsonl(config.CHUNKS_FILE)
    if not chunks:
        raise ValueError("No chunks found. Run chunking first.")

    # Deduplicate defensively by chunk_hash.
    seen, unique = set(), []
    for ch in chunks:
        h = ch.get("chunk_hash") or ch.get("chunk_id")
        if h in seen:
            continue
        seen.add(h)
        unique.append(ch)
    if len(unique) != len(chunks):
        logger.info(f"Removed {len(chunks) - len(unique)} duplicate chunks before indexing")
    chunks = unique

    # Final safety net: repair any mojibake so both the embedded text and the
    # persisted metadata are clean (the FAISS index is built from these).
    chunks = [clean_chunk_metadata(ch) for ch in chunks]

    n_docs = len({ch.get("document_id") or ch.get("doc_id") for ch in chunks})
    from src.utils import chunk_search_text
    texts = [chunk_search_text(ch) for ch in chunks]   # metadata header + body

    logger.info(f"Embedding {len(texts)} chunks from {n_docs} documents "
                f"with {config.EMBEDDING_MODEL}…")
    if progress_cb:
        progress_cb(f"Embedding {len(texts)} chunks from {n_docs} documents…")

    embeddings = embed_texts(texts, show_progress=True)
    dim = int(embeddings.shape[1])

    index = faiss.IndexFlatIP(dim)   # cosine via inner product (vectors normalized)
    index.add(embeddings)

    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(config.FAISS_INDEX))

    # Metadata parallel to vectors (keep full text for context rendering).
    metadata = [dict(ch) for ch in chunks]
    with open(config.METADATA_FILE, "wb") as f:
        pickle.dump(metadata, f)
    with open(METADATA_JSON, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)

    # Per-source breakdown for the log.
    by_source: Dict[str, int] = {}
    for ch in chunks:
        s = ch.get("source", "unknown")
        by_source[s] = by_source.get(s, 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))

    logger.info(f"FAISS index saved: {len(chunks)} vectors (dim={dim}) "
                f"from {n_docs} docs [{breakdown}]")
    if progress_cb:
        progress_cb(f"✓ FAISS index built — {len(chunks)} chunks from {n_docs} docs ({breakdown})")

    # Reset cache so the next search loads fresh data.
    global _INDEX, _METADATA
    _INDEX, _METADATA = None, []
    return len(chunks), dim


# ── Load ───────────────────────────────────────────────────────────────────────

_INDEX = None
_METADATA: List[Dict] = []


def load_index(force: bool = False):
    """Load FAISS index and metadata into the module-level cache."""
    global _INDEX, _METADATA
    if _INDEX is not None and not force:
        return
    import faiss

    if not config.FAISS_INDEX.exists():
        raise FileNotFoundError(
            f"FAISS index not found at {config.FAISS_INDEX}. Run build first."
        )
    _INDEX = faiss.read_index(str(config.FAISS_INDEX))

    if config.METADATA_FILE.exists():
        with open(config.METADATA_FILE, "rb") as f:
            _METADATA = pickle.load(f)
    elif METADATA_JSON.exists():
        with open(METADATA_JSON, "r", encoding="utf-8") as f:
            _METADATA = json.load(f)
    else:
        raise FileNotFoundError("Index metadata not found.")

    logger.info(f"Index loaded: {_INDEX.ntotal} vectors, {len(_METADATA)} metadata records")


def is_loaded() -> bool:
    """True if the FAISS index + metadata are already in memory."""
    return _INDEX is not None


def ntotal() -> int:
    """Number of vectors in the loaded index (0 if not loaded yet)."""
    return int(_INDEX.ntotal) if _INDEX is not None else 0


def get_metadata() -> List[Dict]:
    """
    Return the in-memory chunk-metadata list (the SINGLE shared copy that backs
    FAISS search). Other components (e.g. the BM25 index) should reuse THIS list
    instead of re-reading chunks.jsonl, so the process keeps exactly one full
    copy of the chunk text in memory rather than two or three.
    """
    load_index()
    return _METADATA


def index_stats() -> Dict:
    """Return basic stats about the loaded index (by source + category)."""
    load_index()
    by_source: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    doc_ids = set()
    for m in _METADATA:
        s = m.get("source", "unknown")
        by_source[s] = by_source.get(s, 0) + 1
        c = m.get("category", "") or "uncategorized"
        by_category[c] = by_category.get(c, 0) + 1
        doc_ids.add(m.get("document_id") or m.get("doc_id"))
    return {
        "total_chunks":    _INDEX.ntotal if _INDEX else 0,
        "total_documents": len(doc_ids),
        "by_source":       by_source,
        "by_category":     by_category,
    }


# ── Search ─────────────────────────────────────────────────────────────────────

def search(
    query: str,
    top_k: int = config.TOP_K,
    source_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
    author_filter: Optional[str] = None,
    year_filter: Optional[str] = None,
    # legacy alias kept so older callers don't break:
    source_type_filter: Optional[str] = None,
) -> List[Dict]:
    """
    FAISS semantic search with optional metadata filters.
    Returns chunk dicts augmented with 'score' (raw cosine similarity).
    """
    load_index()
    if source_filter is None and source_type_filter is not None:
        source_filter = source_type_filter

    q_vec = embed_query(query)
    k = min(max(top_k * 12, 30), _INDEX.ntotal)
    scores, indices = _INDEX.search(q_vec, k)

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(_METADATA):
            continue
        meta = _METADATA[idx]

        if source_filter and source_filter != "all":
            ms = (meta.get("source") or meta.get("source_type") or "").lower()
            if ms != source_filter.lower():
                continue
        if category_filter and category_filter != "all":
            mc = (meta.get("category") or "").lower()
            if category_filter.lower() not in mc:
                continue
        if author_filter:
            if author_filter.lower() not in (meta.get("author") or "").lower():
                continue
        if year_filter:
            if not (meta.get("date") or "").startswith(year_filter):
                continue

        results.append({**meta, "score": float(score)})

    return results


def update_index_with_new_chunks(new_chunk_texts: List[str],
                                 new_metadata: List[Dict],
                                 progress_cb=None) -> int:
    """Incrementally add new chunks to an existing FAISS index."""
    import faiss

    load_index()
    # Embed the metadata-aware search text (header + body) to match the rest of
    # the pipeline, so any chunk added this way is retrievable by its metadata too.
    from src.utils import chunk_search_text
    search_texts = [
        chunk_search_text({**(new_metadata[i] if i < len(new_metadata) else {}), "text": t})
        for i, t in enumerate(new_chunk_texts)
    ]
    embeddings = embed_texts(search_texts)
    _INDEX.add(embeddings)
    _METADATA.extend(new_metadata)

    faiss.write_index(_INDEX, str(config.FAISS_INDEX))
    with open(config.METADATA_FILE, "wb") as f:
        pickle.dump(_METADATA, f)
    with open(METADATA_JSON, "w", encoding="utf-8") as f:
        json.dump(_METADATA, f, ensure_ascii=False)

    logger.info(f"Index updated: now {_INDEX.ntotal} vectors")
    if progress_cb:
        progress_cb(f"✓ Index updated — {_INDEX.ntotal} total chunks")
    return _INDEX.ntotal