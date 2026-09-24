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
    bm25: object = None                 # CompactBM25 (rank_bm25-identical scores) or None
    version: str = ""
    root: str = ""
    loaded_at: float = field(default_factory=time.time)

    @property
    def ntotal(self) -> int:
        return int(self.index.ntotal) if self.index is not None else 0


_STATE: Optional[KBState] = None
_LOAD_LOCK = threading.Lock()
_SUSPENDED = False          # low-memory release swap in progress (see kb_sync)


class KBReloading(RuntimeError):
    """Raised while a low-memory release swap has no serving state (callers → 503)."""


# ── Persistence helpers ─────────────────────────────────────────────────────────
def _iter_json_array(path: Path, block: int = 1 << 20):
    """
    Yield the elements of a JSON array file one at a time. Equivalent to
    ``json.load`` but without holding the whole file as one string, which keeps the
    loading peak far below the file size (matters on 512 MB hosts).
    """
    dec = json.JSONDecoder()
    buf, pos, started = "", 0, False
    with open(path, "r", encoding="utf-8") as f:
        while True:
            # skip whitespace and separators
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n,":
                    pos += 1
                if pos < len(buf):
                    break
                more = f.read(block)
                if not more:
                    return
                buf, pos = buf[pos:] + more, 0
            if not started:
                if buf[pos] != "[":
                    raise ValueError(f"{path.name}: expected a JSON array")
                started, pos = True, pos + 1
                continue
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    more = f.read(block)
                    if not more:
                        raise
                    buf, pos = buf[pos:] + more, 0
            yield obj
            pos = end
            if pos > block:                       # drop consumed text
                buf, pos = buf[pos:], 0


def _share_values(items, keep_unique=("text", "meta_header")):
    """
    33k chunks come from ~2k documents, so most field values (url, title, authors,
    categories…) repeat. Re-use one object per distinct value: ~half the memory,
    identical content. Values are never mutated in place (results are copies).
    """
    pool: Dict = {}
    for m in items:
        for k, v in m.items():
            if k in keep_unique:
                continue
            if isinstance(v, str):
                m[k] = pool.setdefault(v, v)
            elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                m[k] = pool.setdefault(("\0list",) + tuple(v), v)
        yield m


def _read_metadata(index_dir: Path) -> List[Dict]:
    meta_json, meta_pkl = index_dir / "metadata.json", index_dir / "metadata.pkl"
    if meta_json.exists():
        return list(_share_values(_iter_json_array(meta_json)))
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
    try:                              # optional serving artifact; the API rebuilds without it
        save_bm25_artifacts(index_dir, metadata)
    except Exception as exc:
        logger.warning(f"Could not precompute BM25 ({exc}); the API will build it at load.")
        for f in ("bm25.json", "bm25.npz", "bm25_vocab.json"):
            (index_dir / f).unlink(missing_ok=True)


class CompactBM25:
    """
    BM25 Okapi with the exact formulas, parameters and floating-point order of
    ``rank_bm25.BM25Okapi`` (k1=1.5, b=0.75, epsilon=0.25; negative idf floored to
    epsilon × average idf) — scores are identical — but stored as flat numpy postings
    (term → docs, tf) instead of one Python dict per chunk: ~10× less memory.
    """

    def __init__(self, corpus, k1: float = 1.5, b: float = 0.75, epsilon: float = 0.25):
        import math
        from array import array
        self.k1, self.b, self.epsilon = k1, b, epsilon
        vocab: Dict[str, int] = {}
        terms, docs, tfs, doc_len = array("i"), array("i"), array("i"), array("i")
        total = 0
        for d, tokens in enumerate(corpus):
            doc_len.append(len(tokens))
            total += len(tokens)
            freqs: Dict[str, int] = {}
            for w in tokens:
                freqs[w] = freqs.get(w, 0) + 1
            for w, c in freqs.items():
                tid = vocab.get(w)
                if tid is None:
                    tid = vocab[w] = len(vocab)
                terms.append(tid)
                docs.append(d)
                tfs.append(c)
        self.corpus_size = len(doc_len)
        self.avgdl = total / self.corpus_size
        self.vocab = vocab
        t = np.frombuffer(terms, dtype=np.int32)
        order = np.argsort(t, kind="stable")                  # docs stay ascending per term
        self.post_docs = np.frombuffer(docs, dtype=np.int32)[order].copy()
        self.post_tf = np.frombuffer(tfs, dtype=np.int32)[order].astype(np.int64)
        df = np.bincount(t, minlength=len(vocab))
        self.ptr = np.concatenate(([0], np.cumsum(df))).astype(np.int64)
        self.doc_len = np.frombuffer(doc_len, dtype=np.int32).astype(np.int64)
        # idf in vocabulary (first-seen) order — the same summation order as rank_bm25.
        idf = np.empty(len(vocab), dtype=np.float64)
        idf_sum, negative = 0.0, []
        for tid in range(len(vocab)):
            freq = int(df[tid])
            v = math.log(self.corpus_size - freq + 0.5) - math.log(freq + 0.5)
            idf[tid] = v
            idf_sum += v
            if v < 0:
                negative.append(tid)
        self.average_idf = idf_sum / len(vocab)
        eps = self.epsilon * self.average_idf
        for tid in negative:
            idf[tid] = eps
        self.idf = idf
        # rank_bm25 evaluates  q_freq + k1 * (1 - b + b * doc_len / avgdl)
        self._norm = self.k1 * (1 - self.b + self.b * self.doc_len / self.avgdl)

    # ── persistence (precomputed at index-build time; saves minutes on a 0.1-CPU start)
    _SCALARS = ("corpus_size", "avgdl", "average_idf", "k1", "b", "epsilon")

    def save(self, index_dir: Path, fingerprint: str) -> None:
        index_dir = Path(index_dir)
        tmp_npz, tmp_voc, tmp_man = (index_dir / "bm25.npz.tmp", index_dir / "bm25_vocab.json.tmp",
                                     index_dir / "bm25.json.tmp")
        with open(tmp_npz, "wb") as f:
            np.savez(f, post_docs=self.post_docs, post_tf=self.post_tf.astype(np.int32), ptr=self.ptr,
                     doc_len=self.doc_len.astype(np.int32), idf=self.idf,
                     scalars=np.array([float(getattr(self, k)) for k in self._SCALARS], dtype=np.float64))
        vocab = [None] * len(self.vocab)
        for w, i in self.vocab.items():
            vocab[i] = w
        tmp_voc.write_text(json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        tmp_man.write_text(json.dumps({"format": BM25_FORMAT, "fingerprint": fingerprint,
                                       "documents": self.corpus_size}), encoding="utf-8")
        tmp_npz.replace(index_dir / "bm25.npz")
        tmp_voc.replace(index_dir / "bm25_vocab.json")
        tmp_man.replace(index_dir / "bm25.json")          # manifest last

    @classmethod
    def load(cls, index_dir: Path) -> "CompactBM25":
        index_dir = Path(index_dir)
        self = cls.__new__(cls)
        with np.load(index_dir / "bm25.npz", allow_pickle=False) as z:
            self.post_docs = z["post_docs"]
            self.post_tf = z["post_tf"].astype(np.int64)
            self.ptr = z["ptr"]
            self.doc_len = z["doc_len"].astype(np.int64)
            self.idf = z["idf"]
            sc = z["scalars"]
        (corpus_size, self.avgdl, self.average_idf, self.k1, self.b, self.epsilon) = [float(x) for x in sc]
        self.corpus_size = int(corpus_size)
        words = json.loads((index_dir / "bm25_vocab.json").read_text(encoding="utf-8"))
        self.vocab = {w: i for i, w in enumerate(words)}
        self._norm = self.k1 * (1 - self.b + self.b * self.doc_len / self.avgdl)
        return self

    def get_scores(self, query: List[str]) -> np.ndarray:
        score = np.zeros(self.corpus_size)
        for q in query:
            tid = self.vocab.get(q)
            if tid is None:
                continue                                        # rank_bm25 adds 0 here
            lo, hi = self.ptr[tid], self.ptr[tid + 1]
            d = self.post_docs[lo:hi]
            f = self.post_tf[lo:hi]
            w = self.idf[tid] or 0
            score[d] += w * (f * (self.k1 + 1) / (f + self._norm[d]))
        return score


BM25_FORMAT = 2


def bm25_fingerprint(index_dir: Path) -> str:
    """
    Identity of what BM25 indexes: the exact bytes of ``metadata.json`` (every chunk's
    text and header) plus the tokenizer rules. A precomputed BM25 is used only if this
    matches, so it can never serve a different metadata file. Streamed: cheap and
    constant-memory even on a 0.1-CPU start.
    """
    import hashlib
    h = hashlib.sha256(f"v{BM25_FORMAT}|{_TOKEN_RE.pattern}|{' '.join(sorted(_BM25_STOP))}|".encode())
    with open(Path(index_dir) / "metadata.json", "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _compute_bm25(metadata: List[Dict]) -> "CompactBM25":
    from src.utils import chunk_search_text
    return CompactBM25(bm25_tokenize(chunk_search_text(ch)) or ["_"] for ch in metadata)


def save_bm25_artifacts(index_dir: Path, metadata: List[Dict]) -> None:
    """Precompute BM25 for a release (after metadata.json is written, or when packed)."""
    if metadata:
        _compute_bm25(metadata).save(Path(index_dir), bm25_fingerprint(index_dir))


def _build_bm25(metadata: List[Dict], index_dir: Optional[Path] = None):
    if not metadata:
        return None
    try:
        if index_dir is not None and (Path(index_dir) / "bm25.json").exists():
            try:
                man = json.loads((Path(index_dir) / "bm25.json").read_text(encoding="utf-8"))
                if (man.get("format") == BM25_FORMAT and man.get("documents") == len(metadata)
                        and man.get("fingerprint") == bm25_fingerprint(index_dir)):
                    return CompactBM25.load(index_dir)
                logger.warning("Precomputed BM25 does not match this release — rebuilding.")
            except Exception as exc:
                logger.warning(f"Precomputed BM25 unreadable ({exc}) — rebuilding.")
        return _compute_bm25(metadata)
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
    if config.KB_LOW_MEMORY and hasattr(faiss, "IO_FLAG_MMAP_IFC"):
        # Memory-map the vectors (file-backed, reclaimable pages; identical results).
        index = faiss.read_index(str(index_path), faiss.IO_FLAG_MMAP_IFC)
    else:
        index = faiss.read_index(str(index_path))
    metadata = _read_metadata(root / "index")
    if index.ntotal != len(metadata):
        raise ValueError(f"Index/metadata mismatch: {index.ntotal} vectors vs {len(metadata)} records")
    t1 = time.perf_counter()
    bm25 = _build_bm25(metadata, root / "index")
    t2 = time.perf_counter()
    manifest = root / "kb_manifest.json"
    if not version and manifest.exists():
        try:
            version = json.loads(manifest.read_text(encoding="utf-8")).get("version", "")
        except Exception:
            version = ""
    logger.info(f"KB state built: {index.ntotal} vectors (index+metadata {t1-t0:.1f}s, "
                f"BM25 {t2-t1:.1f}s) version={version or 'unversioned'}")
    release_free_memory()
    return KBState(index=index, metadata=metadata, bm25=bm25, version=version, root=str(root))


def release_free_memory() -> None:
    """Return freed heap to the OS after large loads (glibc only; no-op elsewhere)."""
    import gc
    gc.collect()
    try:
        import ctypes
        import sys
        if sys.platform.startswith("linux"):
            ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def set_state(state: KBState) -> None:
    """Atomically make ``state`` the serving state."""
    global _STATE, _SUSPENDED
    _STATE = state
    _SUSPENDED = False


def suspend() -> None:
    """Release the serving state for a low-memory swap; loads are refused until set_state."""
    global _STATE, _SUSPENDED
    _SUSPENDED = True
    _STATE = None


def resume() -> None:
    global _SUSPENDED
    _SUSPENDED = False


def load_index(force: bool = False) -> KBState:
    """Load (once) and return the serving state."""
    global _STATE
    if _STATE is not None and not force:
        return _STATE
    if _SUSPENDED and not force:
        raise KBReloading("The knowledge base is being updated. Try again in a minute.")
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
