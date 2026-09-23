"""
vector_store.py — FAISS + BM25 serving state, build, save and atomic reload.

The searchable knowledge base is held in ONE immutable :class:`KBState`
(FAISS index + chunk metadata + BM25 index + version). Queries grab a reference
to the current state once and use it for the whole request; a reload builds a
complete new state off to the side and then swaps a single module reference.
A query therefore never sees a new FAISS index paired with old metadata (or a
half-built BM25), and a failed reload leaves the previous state serving.

Metadata is persisted as JSON. The legacy ``metadata.pkl`` is written no more
and only read when ``ALLOW_PICKLE_METADATA=true`` (unpickling a tampered file
can execute code).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from src import config
from src.utils import clean_chunk_metadata, get_logger, load_jsonl

logger = get_logger("vector_store", config.SCRAPE_LOG)

# Kept for backward compatibility with modules that imported it.
METADATA_JSON = config.METADATA_JSON

# ── BM25 tokenisation ────────────────────────────────────────────────────────────
_BM25_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this "
    "to was were will with what who when where which how why do does did".split())
_TOKEN_RE = re.compile(r"[a-z0-9À-ɏऀ-ॿ]+")


def bm25_tokenize(text: str) -> List[str]:
    """Lower-case word tokens without punctuation ('AI,' == 'ai'), minus stopwords."""
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _BM25_STOP]


@dataclass(frozen=True)
class KBState:
    index: object                       # faiss.Index
    metadata: List[Dict]
    bm25: object = None                 # rank_bm25.BM25Okapi or None
    version: str = ""
    root: str = ""
    loaded_at: float = field(default_factory=time.time)

    @property
    def ntotal(self) -> int:
        return int(self.index.ntotal) if self.index is not None else 0


_STATE: Optional[KBState] = None
_LOAD_LOCK = threading.Lock()


# ── Persistence helpers ─────────────────────────────────────────────────────────
def _read_metadata(index_dir: Path) -> List[Dict]:
    meta_json, meta_pkl = index_dir / "metadata.json", index_dir / "metadata.pkl"
    if meta_json.exists():
        with open(meta_json, "r", encoding="utf-8") as f:
            return json.load(f)
    if config.ALLOW_PICKLE_METADATA and meta_pkl.exists():
        import pickle
        logger.warning("Loading legacy metadata.pkl (ALLOW_PICKLE_METADATA=true).")
        with open(meta_pkl, "rb") as f:
            return pickle.load(f)
    raise FileNotFoundError(f"Index metadata not found at {meta_json}.")


def write_index_files(index, metadata: List[Dict], index_dir=None) -> None:
    """Write faiss.index + metadata.json into ``index_dir`` via temp files + rename."""
    import faiss
    index_dir = index_dir or config.INDEX_DIR
    index_dir.mkdir(parents=True, exist_ok=True)
    tmp_idx = index_dir / "faiss.index.tmp"
    tmp_meta = index_dir / "metadata.json.tmp"
    faiss.write_index(index, str(tmp_idx))
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)
    tmp_idx.replace(index_dir / "faiss.index")
    tmp_meta.replace(index_dir / "metadata.json")
    stale_pkl = index_dir / "metadata.pkl"
    if stale_pkl.exists():            # never leave a pickle that disagrees with JSON
        stale_pkl.unlink()


def _build_bm25(metadata: List[Dict]):
    if not metadata:
        return None
    try:
        from rank_bm25 import BM25Okapi
        from src.utils import chunk_search_text
        return BM25Okapi([bm25_tokenize(chunk_search_text(ch)) or ["_"] for ch in metadata])
    except Exception as exc:
        logger.warning(f"BM25 build failed (FAISS-only retrieval): {exc}")
        return None


def build_state(version: str = "", root: Optional[Path] = None) -> KBState:
    """Load FAISS + metadata from ``root`` (default: active KB root) and build BM25. No swap."""
    import faiss
    root = Path(root) if root else config.KB_ROOT
    index_path = root / "index" / "faiss.index"
    if not index_path.exists():
        raise FileNotFoundError(f"FAISS index not found at {index_path}. Run a build first.")
    t0 = time.perf_counter()
    index = faiss.read_index(str(index_path))
    metadata = _read_metadata(root / "index")
    if index.ntotal != len(metadata):
        raise ValueError(f"Index/metadata mismatch: {index.ntotal} vectors vs {len(metadata)} records")
    t1 = time.perf_counter()
    bm25 = _build_bm25(metadata)
    t2 = time.perf_counter()
    manifest = root / "kb_manifest.json"
    if not version and manifest.exists():
        try:
            version = json.loads(manifest.read_text(encoding="utf-8")).get("version", "")
        except Exception:
            version = ""
    logger.info(f"KB state built: {index.ntotal} vectors (index+metadata {t1-t0:.1f}s, "
                f"BM25 {t2-t1:.1f}s) version={version or 'unversioned'}")
    return KBState(index=index, metadata=metadata, bm25=bm25, version=version, root=str(root))


def set_state(state: KBState) -> None:
    """Atomically make ``state`` the serving state."""
    global _STATE
    _STATE = state


def load_index(force: bool = False) -> KBState:
    """Load (once) and return the serving state."""
    global _STATE
    if _STATE is not None and not force:
        return _STATE
    with _LOAD_LOCK:
        if _STATE is not None and not force:
            return _STATE
        _STATE = build_state()
    return _STATE


def get_state() -> KBState:
    return load_index()


def reset() -> None:
    """Drop the in-memory state (next access reloads from disk)."""
    global _STATE
    _STATE = None


def is_loaded() -> bool:
    return _STATE is not None


def ntotal() -> int:
    return _STATE.ntotal if _STATE is not None else 0


def get_metadata() -> List[Dict]:
    return load_index().metadata


def index_stats() -> Dict:
    st = load_index()
    by_source: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    doc_ids = set()
    for m in st.metadata:
        s = m.get("source", "unknown")
        by_source[s] = by_source.get(s, 0) + 1
        c = m.get("category", "") or "uncategorized"
        by_category[c] = by_category.get(c, 0) + 1
        t = m.get("content_type", "") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1
        doc_ids.add(m.get("document_id") or m.get("doc_id"))
    return {"total_chunks": st.ntotal, "total_documents": len(doc_ids),
            "by_source": by_source, "by_category": by_category,
            "by_content_type": by_type, "version": st.version}


# ── Filtering ─────────────────────────────────────────────────────────────────────
def _norm_list(v) -> List[str]:
    if not v:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).lower() for x in v if x]
    return [str(v).lower()]


def matches_filters(meta: Dict, *, sources=None, category=None, author=None,
                    year=None, content_type=None) -> bool:
    if sources:
        ms = (meta.get("source") or meta.get("source_type") or "").lower()
        if ms not in sources:
            return False
    if category and category != "all":
        cats = " | ".join(_norm_list(meta.get("categories")) + _norm_list(meta.get("category")))
        if category.lower() not in cats:
            return False
    if author:
        names = _norm_list(meta.get("authors")) + _norm_list(meta.get("author"))
        if not any(author.lower() in n for n in names):
            return False
    if year:
        d = str(meta.get("publication_date") or meta.get("date") or "")
        if not d.startswith(str(year)):
            return False
    if content_type and content_type != "all":
        wanted = {c.strip().lower() for c in str(content_type).split(",")}
        if (meta.get("content_type") or "").lower() not in wanted:
            return False
    return True


def _sources_set(source_filter) -> Optional[set]:
    if not source_filter or source_filter == "all":
        return None
    if isinstance(source_filter, (list, tuple, set, frozenset)):
        return {s.lower() for s in source_filter}
    return {str(source_filter).lower()}


# ── Search ────────────────────────────────────────────────────────────────────────
def search(query: str, top_k: int = config.TOP_K, source_filter=None,
           category_filter: Optional[str] = None, author_filter: Optional[str] = None,
           year_filter: Optional[str] = None, content_type_filter: Optional[str] = None,
           source_type_filter: Optional[str] = None, state: Optional[KBState] = None,
           query_vector: Optional[np.ndarray] = None) -> List[Dict]:
    """FAISS semantic search; returns chunk dicts with a raw cosine ``score``."""
    st = state or load_index()
    if st.ntotal == 0:
        return []
    if source_filter is None and source_type_filter is not None:
        source_filter = source_type_filter
    sources = _sources_set(source_filter)
    filtered = bool(sources or category_filter or author_filter or year_filter or content_type_filter)

    if query_vector is None:
        from src.embeddings import embed_query
        query_vector = embed_query(query)
    # Filters are applied after search, so a filtered query scans the whole
    # (flat, exact) index rather than silently returning too few hits.
    k = st.ntotal if filtered else min(max(top_k * 12, 60), st.ntotal)
    scores, indices = st.index.search(query_vector, k)

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(st.metadata):
            continue
        meta = st.metadata[idx]
        if filtered and not matches_filters(meta, sources=sources, category=category_filter,
                                            author=author_filter, year=year_filter,
                                            content_type=content_type_filter):
            continue
        results.append({**meta, "score": float(score), "_row": int(idx)})
        if len(results) >= max(top_k * 12, 60):
            break
    return results


def bm25_search(query: str, top_k: int = 20, *, state: Optional[KBState] = None,
                source_filter=None, category_filter=None, author_filter=None,
                year_filter=None, content_type_filter=None) -> List[Dict]:
    st = state or load_index()
    if st.bm25 is None:
        return []
    toks = bm25_tokenize(query)
    if not toks:
        return []
    scores = st.bm25.get_scores(toks)
    sources = _sources_set(source_filter)
    filtered = bool(sources or category_filter or author_filter or year_filter or content_type_filter)
    order = np.argsort(-scores, kind="stable")
    out = []
    for idx in order:
        s = float(scores[idx])
        if s <= 0:
            break
        meta = st.metadata[idx]
        if filtered and not matches_filters(meta, sources=sources, category=category_filter,
                                            author=author_filter, year=year_filter,
                                            content_type=content_type_filter):
            continue
        out.append({**meta, "bm25_score": s, "_row": int(idx)})
        if len(out) >= top_k:
            break
    return out


# ── Build (full, no cache) ─────────────────────────────────────────────────────────
def build_index(progress_cb=None, chunks: Optional[List[Dict]] = None) -> Tuple[int, int]:
    """Embed all chunks and write a fresh index (prefer incremental_index.rebuild_index)."""
    import faiss
    from src.embeddings import embed_texts
    from src.utils import chunk_search_text

    if chunks is None:
        chunks = load_jsonl(config.CHUNKS_FILE)
    if not chunks:
        raise ValueError("No chunks found. Run chunking first.")
    seen, unique = set(), []
    for ch in chunks:
        h = ch.get("chunk_hash") or ch.get("chunk_id")
        if h in seen:
            continue
        seen.add(h)
        unique.append(clean_chunk_metadata(ch))
    embeddings = embed_texts([chunk_search_text(ch) for ch in unique], show_progress=True)
    index = faiss.IndexFlatIP(int(embeddings.shape[1]))
    index.add(embeddings)
    write_index_files(index, unique)
    if progress_cb:
        progress_cb(f"✓ FAISS index built — {len(unique)} chunks")
    reset()
    return len(unique), int(embeddings.shape[1])
