"""
incremental_index.py — Merge document deltas and rebuild the index cheaply.

Two jobs, both designed to keep the *existing* on-disk format byte-compatible so
the retriever, the Streamlit app and the Mattermost bot keep working unchanged:

1. :func:`merge_documents` — apply a crawl delta to
   ``data/processed/documents.jsonl`` **in place**: replace records whose
   ``document_id`` changed, add new ones, and drop removed ones. (The old scraper
   only ever appended, so changed pages were never updated — this fixes that.)

2. :func:`rebuild_index` — re-chunk the unified documents and rebuild the FAISS
   index, but embed **only new/changed chunks** via :class:`EmbeddingCache`. The
   output files (``faiss.index`` + ``metadata.pkl`` + ``metadata.json``) are the
   same ones ``vector_store.build_index`` writes, so nothing downstream changes.

Because ``IndexFlatIP`` can't remove individual vectors, a change/removal triggers
a rebuild — but with the embedding cache that rebuild only pays for the chunks
that actually changed, so an incremental update stays fast end-to-end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src import config
from src.utils import get_logger, load_jsonl, save_jsonl

logger = get_logger("incremental_index", config.SCRAPE_LOG)



# ════════════════════════════════════════════════════════════════════════════════
#  Document delta merge
# ════════════════════════════════════════════════════════════════════════════════

def _doc_key(doc: Dict) -> str:
    return doc.get("document_id") or doc.get("id") or doc.get("url_hash") or doc.get("url") or ""


def _norm_url(u: str) -> str:
    """Normalise a URL for identity comparison (same canonical form as the crawler)."""
    if not u:
        return ""
    from src.url_utils import canonicalize_url
    cu = canonicalize_url(u.strip())
    if cu:
        return cu.rstrip("/") or cu
    try:
        from urllib.parse import urlsplit, urlunsplit
        s = urlsplit(u.strip())
        scheme = (s.scheme or "https").lower()
        netloc = s.netloc.lower()
        path = s.path.rstrip("/") or "/"
        # drop fragment; keep query (rarely identity-bearing here but safe to keep)
        return urlunsplit((scheme, netloc, path, s.query, ""))
    except Exception:
        return u.strip().rstrip("/").lower()


def _doc_richness(doc: Dict) -> tuple:
    """Sort key: prefer the copy with the most metadata / content, then newest."""
    has_author = 1 if (str(doc.get("author") or "").strip() or doc.get("authors")) else 0
    has_date = 1 if str(doc.get("date") or "").strip() else 0
    has_section = 1 if str(doc.get("section") or doc.get("category") or "").strip() else 0
    n_tags = len(doc.get("tags") or [])
    text_len = doc.get("text_length") or len(doc.get("text") or "")
    scraped = str(doc.get("scraped_at") or doc.get("updated_at") or "")
    # non-PDF pages are preferred as the canonical holder of a URL over a "[PDF]" twin
    is_html = 0 if (doc.get("source_type") == "pdf" or doc.get("pdf_url")) else 1
    return (has_author, has_date, has_section, n_tags, is_html, text_len, scraped)


def collapse_by_url(docs: List[Dict]) -> tuple:
    """
    Collapse documents that point at the same (normalised) URL, keeping the
    richest copy of each. Returns (kept_docs, dropped_count).

    A page's identity is its canonical URL when present, else its URL. This
    removes the "same page stored more than once" duplicates that accumulate when
    an older document-id scheme and a newer one both wrote the same page.
    """
    groups: Dict[str, List[Dict]] = {}
    order: List[str] = []
    for d in docs:
        key = _norm_url(d.get("canonical_url") or d.get("url") or d.get("original_url") or "")
        if not key:
            key = _doc_key(d) or f"__noid_{len(order)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(d)

    kept: List[Dict] = []
    dropped = 0
    for key in order:
        bucket = groups[key]
        if len(bucket) == 1:
            kept.append(bucket[0])
            continue
        best = max(bucket, key=_doc_richness)
        kept.append(best)
        dropped += len(bucket) - 1
    return kept, dropped


def merge_documents(new_or_changed: List[Dict],
                    removed_ids: Optional[Iterable[str]] = None,
                    documents_file: Path = None) -> Dict[str, int]:
    """
    Merge a crawl delta into ``documents.jsonl`` in place, keyed by document_id.

    * records in ``new_or_changed`` replace any existing record with the same id,
      or are appended if new;
    * any id in ``removed_ids`` is dropped.

    Returns a summary dict {added, updated, removed, total}.
    """
    documents_file = documents_file or config.DOCUMENTS_FILE
    removed_ids = set(removed_ids or [])

    existing = load_jsonl(documents_file)
    by_id: Dict[str, Dict] = {}
    order: List[str] = []
    for d in existing:
        k = _doc_key(d)
        if not k:
            continue
        if k not in by_id:
            order.append(k)
        by_id[k] = d

    added = updated = 0
    for doc in new_or_changed:
        k = _doc_key(doc)
        if not k:
            continue
        if k in by_id:
            updated += 1
        else:
            added += 1
            order.append(k)
        by_id[k] = doc

    removed = 0
    for k in list(removed_ids):
        if k in by_id:
            by_id.pop(k, None)
            removed += 1

    merged = [by_id[k] for k in order if k in by_id]

    # Collapse any same-URL duplicates (e.g. legacy documents written under a
    # different id scheme for a page the new crawler also stored). Keeps the
    # richest copy of each URL so a page is never stored more than once.
    merged, url_dupes = collapse_by_url(merged)

    save_jsonl(documents_file, merged)

    summary = {"added": added, "updated": updated, "removed": removed,
               "url_duplicates_collapsed": url_dupes, "total": len(merged)}
    logger.info(f"documents.jsonl merged: {summary}")
    return summary


# ════════════════════════════════════════════════════════════════════════════════
#  Cached index rebuild
# ════════════════════════════════════════════════════════════════════════════════

_PIPELINE_MODULES = ("chunker.py", "metadata.py", "supplementary.py", "people.py", "utils.py",
                     "url_utils.py")


def pipeline_version() -> str:
    """
    Fingerprint of everything that turns documents into indexed chunks (processing
    code, chunk parameters, embedding model). Stored in kb_manifest.json; when it
    changes, the next refresh re-indexes even if no document changed, so a deployed
    processing fix reaches production. Unchanged chunks still come from the cache.
    """
    import hashlib
    h = hashlib.sha256()
    src_dir = Path(__file__).resolve().parent
    for name in _PIPELINE_MODULES:
        h.update(name.encode())
        h.update((src_dir / name).read_bytes().replace(b"\r\n", b"\n"))
    h.update(f"{config.CHUNK_SIZE}|{config.CHUNK_OVERLAP}|{config.CHUNK_MIN_LEN}|"
             f"{config.EMBEDDING_MODEL}".encode())
    return h.hexdigest()[:16]


def load_all_documents() -> List[Dict]:
    """documents.jsonl + curated supplementary files, normalised to the schema."""
    from src.metadata import normalize_document
    from src.supplementary import load_supplementary_documents
    crawled = load_jsonl(config.DOCUMENTS_FILE)
    known_people = [d.get("title") for d in crawled if d.get("content_type") == "person"]
    docs = [normalize_document(d, known_people) for d in crawled]
    have = {d["document_id"] for d in docs}
    docs += [normalize_document(d) for d in load_supplementary_documents() if d["document_id"] not in have]
    return docs


def write_kb_manifest(summary: Dict, version: Optional[str] = None) -> Dict:
    """Version stamp of the KB root (read by the API's /health and kb_sync)."""
    version = version or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = {"version": version, "built_at": datetime.now(timezone.utc).isoformat(), **summary}
    config.KB_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.KB_MANIFEST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.KB_MANIFEST_FILE)
    return manifest


def rebuild_index(progress_cb=None, use_cache: bool = True, version: Optional[str] = None) -> Dict[str, int]:
    """
    Re-chunk all documents and rebuild FAISS for the ACTIVE KB root, embedding only
    new/changed chunks (embedding cache keyed by chunk content hash). Writes
    faiss.index + metadata.json atomically, the people graph and kb_manifest.json.

    Returns {documents, chunks, embedded, cached, new_chunks, removed_chunks,
    by_source, version}. ``embedded`` == chunks whose text/metadata is new or changed.
    """
    import faiss  # lazy: heavy
    from src.chunker import chunk_documents
    from src.people import build_people_graph, save_people_graph
    from src.utils import clean_chunk_metadata
    from src.vector_store import write_index_files, reset

    docs = load_all_documents()
    if not docs:
        raise ValueError(f"No documents found at {config.DOCUMENTS_FILE}. Crawl first.")

    old_hashes = set()
    if config.CHUNKS_FILE.exists():
        old_hashes = {c.get("chunk_hash") for c in load_jsonl(config.CHUNKS_FILE)}

    if progress_cb:
        progress_cb(f"Chunking {len(docs)} documents…")
    chunks = [clean_chunk_metadata(ch) for ch in chunk_documents(docs, progress_cb=progress_cb)]
    if not chunks:
        raise ValueError("No chunks produced from the documents.")
    save_jsonl(config.CHUNKS_FILE, chunks)
    new_hashes = {c.get("chunk_hash") for c in chunks}

    if use_cache:
        from src.embedding_cache import EmbeddingCache
        cache = EmbeddingCache()
        embeddings, estats = cache.embed_chunks(chunks, show_progress=False)
        cache.prune(new_hashes)
        cache.save()
    else:
        from src.embeddings import embed_texts
        from src.utils import chunk_search_text
        embeddings = embed_texts([chunk_search_text(ch) for ch in chunks])
        estats = {"embedded": len(chunks), "cached": 0}

    index = faiss.IndexFlatIP(int(embeddings.shape[1]))
    index.add(embeddings.astype("float32"))
    write_index_files(index, [dict(ch) for ch in chunks])
    reset()   # next query in THIS process reloads (servers swap via kb_sync instead)

    save_people_graph(build_people_graph(docs))

    by_source: Dict[str, int] = {}
    for ch in chunks:
        by_source[ch.get("source", "unknown")] = by_source.get(ch.get("source", "unknown"), 0) + 1
    summary = {
        "documents": len({ch.get("document_id") for ch in chunks}),
        "chunks": len(chunks),
        "embedded": estats["embedded"], "cached": estats["cached"],
        "new_chunks": len(new_hashes - old_hashes) if old_hashes else len(new_hashes),
        "removed_chunks": len(old_hashes - new_hashes) if old_hashes else 0,
        "by_source": by_source,
        "embedding_model": config.EMBEDDING_MODEL,
        "pipeline_version": pipeline_version(),
    }
    summary["version"] = write_kb_manifest(summary, version)["version"]
    logger.info(f"Index rebuilt: {summary}")
    if progress_cb:
        progress_cb(f"✓ Index rebuilt — {len(chunks)} chunks from {summary['documents']} docs "
                    f"(embedded {estats['embedded']}, reused {estats['cached']})")
    return summary
