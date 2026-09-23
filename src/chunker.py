"""
chunker.py — Sentence-aware, character-based chunking with full metadata.

Each chunk carries:
    document_id, chunk_id, chunk_index, source, source_name,
    title, category, url, text
plus legacy mirror fields (doc_id, source_type, original_url, author, date,
pdf_url, page_number, chunk_hash) so older modules keep working.

Chunk size is configured in CHARACTERS (CHUNK_SIZE ~800–1200) with overlap
(CHUNK_OVERLAP ~150–250). Sentences are kept whole whenever possible.
"""

import re
from typing import Dict, List

from src import config
from src.utils import (
    clean_text, clean_document_metadata, clean_mojibake_text,
    clean_or_drop_bad_lines, get_text_quality_score, CHUNK_QUALITY_MIN,
    content_hash, get_logger, load_jsonl, save_jsonl,
    build_meta_header, chunk_search_text,
)

logger = get_logger("chunker", config.SCRAPE_LOG)


# ── Sentence splitting ──────────────────────────────────────────────────────────

def _split_sentences(text: str) -> List[str]:
    """
    Split text into sentence-like units. Newlines are treated as soft breaks
    (the Commit KB text is line-structured), and we also split on . ! ?
    followed by whitespace.
    """
    # Normalize whitespace but keep single newlines as separators.
    text = re.sub(r"[ \t]+", " ", text)
    # Break on blank lines first, then on sentence punctuation.
    rough = re.split(r"\n+|(?<=[.!?])\s+", text)
    return [s.strip() for s in rough if s and s.strip()]


def _pack_sentences(
    sentences: List[str],
    max_chars: int,
    overlap_chars: int,
    min_len: int,
) -> List[str]:
    """
    Greedily pack sentences into chunks up to max_chars. Adjacent chunks share
    ~overlap_chars worth of trailing sentences so context isn't lost at edges.
    Oversized single sentences are hard-split on whitespace.
    """
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    def flush():
        nonlocal current, current_len
        if current:
            joined = " ".join(current).strip()
            if len(joined) >= min_len:
                chunks.append(joined)
        # build overlap tail
        tail: List[str] = []
        tail_len = 0
        for s in reversed(current):
            if tail_len + len(s) + 1 <= overlap_chars:
                tail.insert(0, s)
                tail_len += len(s) + 1
            else:
                break
        current = tail[:]
        current_len = sum(len(s) + 1 for s in current)

    for sent in sentences:
        # Hard-split a sentence that alone exceeds the budget.
        if len(sent) > max_chars:
            if current:
                flush()
            words = sent.split(" ")
            buf = ""
            for w in words:
                if len(buf) + len(w) + 1 > max_chars:
                    if len(buf) >= min_len:
                        chunks.append(buf.strip())
                    buf = w
                else:
                    buf = f"{buf} {w}".strip()
            if buf:
                current.append(buf)
                current_len = len(buf)
            continue

        if current_len + len(sent) + 1 > max_chars and current:
            flush()

        current.append(sent)
        current_len += len(sent) + 1

    if current:
        joined = " ".join(current).strip()
        if len(joined) >= min_len:
            chunks.append(joined)

    # De-dup consecutive identical chunks that can arise from overlap edge cases.
    deduped: List[str] = []
    for c in chunks:
        if not deduped or deduped[-1] != c:
            deduped.append(c)
    return deduped


# ── Document → chunks ────────────────────────────────────────────────────────────

# Metadata propagated from the document to every chunk (small fields only — link
# lists, per-page PDF text and profile work lists stay on the document).
_PROPAGATE = (
    "content_type", "categories", "publication_date", "modified_date", "author_urls",
    "author_url", "source_priority", "parent_url", "is_pdf", "document_series",
    "document_version", "subcategory", "crawl_date", "role", "research_areas",
    "source_file", "http_status", "publisher", "is_external_reference",
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _base_meta(doc: Dict) -> Dict:
    document_id = doc.get("document_id") or doc.get("id") or doc.get("url_hash") or ""
    source      = doc.get("source") or doc.get("source_type") or "local"
    url         = doc.get("url") or doc.get("original_url") or ""
    authors     = doc.get("authors") or ([doc["author"]] if doc.get("author") else [])
    meta = {
        "document_id":  document_id,
        "source":       source,
        "source_name":  doc.get("source_name") or config.source_display_name(source, source),
        "title":        doc.get("title", "") or "Untitled",
        "subtitle":     doc.get("subtitle", "") or "",
        "description":  (doc.get("description", "") or "")[:400],
        "category":     doc.get("category", "") or "",
        "section":      doc.get("section", "") or "",
        "url":          url,
        "canonical_url": doc.get("canonical_url", "") or url,
        "document_type": doc.get("document_type", "") or doc.get("content_type", "") or "",
        "language":     doc.get("language", "") or "",
        "page_id":      doc.get("page_id", "") or "",
        "updated_date": doc.get("updated_date", "") or doc.get("modified_date", "") or "",
        "breadcrumbs":  doc.get("breadcrumbs", []) or [],
        "authors":      authors,
        # ── legacy mirrors ──
        "doc_id":       document_id,
        "source_type":  doc.get("source_type") or source,
        "original_url": url,
        "pdf_url":      doc.get("pdf_url", "") or "",
        "author":       doc.get("author", "") or (authors[0] if authors else ""),
        "date":         doc.get("date", "") or doc.get("publication_date", "") or "",
        "tags":         doc.get("tags", []) or [],
    }
    for k in _PROPAGATE:
        if doc.get(k) not in (None, "", []):
            meta[k] = doc[k]
    # Explicit audience marker (not part of the embedded header): Commit KB content is
    # internal and must only be served to staff scopes; everything else is public.
    meta["access"] = "internal" if meta.get("source") == "commit_kb" else "public"
    meta["meta_header"] = build_meta_header(meta)
    return meta


def _sections(text: str) -> List[tuple]:
    """Split Markdown-headed text into (heading_path, body) sections."""
    sections: List[tuple] = []
    stack: List[tuple] = []          # (level, heading)
    buf: List[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join(h for _, h in stack), body))
        buf.clear()

    for line in text.split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m:
            flush()
            level, heading = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            continue
        buf.append(line)
    flush()
    return sections or [("", text)]


def _make_chunk(base: Dict, chunk_id: str, idx: int, text: str, heading: str,
                page_number=None) -> Dict:
    ch = {**base, "chunk_id": chunk_id, "chunk_index": idx, "chunk_order": idx,
          "heading_path": heading or base.get("section") or "",
          "page_number": page_number, "text": text}
    ch["chunk_hash"] = content_hash(chunk_search_text(ch))
    return ch


def chunk_document(doc: Dict) -> List[Dict]:
    """Chunk one unified document into metadata-rich, section-aware chunks."""
    doc  = clean_document_metadata(doc)   # repair mojibake before splitting
    base = _base_meta(doc)
    chunks: List[Dict] = []
    max_chars, overlap_chars, min_len = config.CHUNK_SIZE, config.CHUNK_OVERLAP, config.CHUNK_MIN_LEN
    # Short-by-nature evidence (op-ed references, holidays) must not be dropped.
    if doc.get("content_type") in ("op-ed", "holiday", "event", "person"):
        min_len = min(min_len, 20)

    if doc.get("pdf_pages"):
        idx = 0
        for page_info in doc["pdf_pages"]:
            page_text, _ = clean_or_drop_bad_lines(clean_mojibake_text(page_info.get("text", "")))
            page_num = page_info.get("page_number", 0)
            if not page_text:
                continue
            for ctext in _pack_sentences(_split_sentences(page_text), max_chars, overlap_chars, min_len):
                if get_text_quality_score(ctext) < CHUNK_QUALITY_MIN:
                    continue
                chunks.append(_make_chunk(base, f"{base['document_id']}_p{page_num}_c{idx}",
                                          idx, ctext, "", page_num))
                idx += 1
        return chunks

    text, _ = clean_or_drop_bad_lines(clean_text(doc.get("text", "")))
    if not text:
        return []
    idx = 0
    for heading, body in _sections(text):
        for ctext in _pack_sentences(_split_sentences(body), max_chars, overlap_chars, min_len):
            if get_text_quality_score(ctext) < CHUNK_QUALITY_MIN:
                continue
            chunks.append(_make_chunk(base, f"{base['document_id']}_c{idx}", idx, ctext, heading))
            idx += 1
    return chunks


# ── Cross-document boilerplate ───────────────────────────────────────────────────
# Template text repeated across many documents (e.g. the "The Takshashila Institution
# is an independent centre for research…" paragraph at the end of every PDF) floods
# generic questions with near-identical chunks from unrelated documents. Lines that
# appear in at least BOILERPLATE_MIN_DOCS different documents are removed before
# chunking, except in content types where that text is the actual subject.
BOILERPLATE_MIN_DOCS = 12
BOILERPLATE_MIN_CHARS = 40
_BOILERPLATE_KEEP_TYPES = ("about", "people_index", "person", "holiday", "op-ed")


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _doc_lines(doc: Dict) -> List[str]:
    texts = [p.get("text", "") for p in doc.get("pdf_pages") or []] or [doc.get("text", "")]
    return [ln for t in texts for ln in (t or "").split("\n")]


def find_boilerplate(docs: List[Dict]) -> set:
    counts: Dict[str, int] = {}
    for d in docs:
        for ln in {_norm_line(x) for x in _doc_lines(d)}:
            if len(ln) >= BOILERPLATE_MIN_CHARS:
                counts[ln] = counts.get(ln, 0) + 1
    return {ln for ln, n in counts.items() if n >= BOILERPLATE_MIN_DOCS}


def strip_boilerplate(doc: Dict, boilerplate: set) -> Dict:
    if not boilerplate or doc.get("content_type") in _BOILERPLATE_KEEP_TYPES:
        return doc

    def clean(t: str) -> str:
        return "\n".join(ln for ln in (t or "").split("\n") if _norm_line(ln) not in boilerplate)

    out = dict(doc)
    out["text"] = clean(doc.get("text", ""))
    if doc.get("pdf_pages"):
        out["pdf_pages"] = [{**p, "text": clean(p.get("text", ""))} for p in doc["pdf_pages"]]
    return out


def chunk_documents(docs: List[Dict], progress_cb=None) -> List[Dict]:
    """Chunk documents (cross-document boilerplate removed), deduplicating by content hash."""
    boilerplate = find_boilerplate(docs) if len(docs) >= BOILERPLATE_MIN_DOCS else set()
    if boilerplate:
        logger.info(f"Removing {len(boilerplate)} boilerplate line(s) repeated across "
                    f"≥{BOILERPLATE_MIN_DOCS} documents")
        docs = [strip_boilerplate(d, boilerplate) for d in docs]
    all_chunks: List[Dict] = []
    seen = set()
    for i, doc in enumerate(docs):
        if not doc.get("text"):
            continue
        for ch in chunk_document(doc):
            if ch["chunk_hash"] in seen:
                continue
            seen.add(ch["chunk_hash"])
            all_chunks.append(ch)
        if progress_cb and i % 20 == 0:
            progress_cb(f"Chunked {i+1}/{len(docs)} documents ({len(all_chunks)} chunks so far)…")
    return all_chunks


def build_chunks(progress_cb=None) -> int:
    """
    Read unified documents from DOCUMENTS_FILE, chunk them, write CHUNKS_FILE.
    Returns total chunk count. (Kept for compatibility with existing scripts.)
    """
    docs = load_jsonl(config.DOCUMENTS_FILE)
    if not docs:
        logger.warning("No documents found to chunk")
        return 0

    logger.info(f"Chunking {len(docs)} documents…")
    all_chunks = chunk_documents(docs, progress_cb=progress_cb)
    save_jsonl(config.CHUNKS_FILE, all_chunks)
    logger.info(f"Saved {len(all_chunks)} chunks to {config.CHUNKS_FILE}")
    if progress_cb:
        progress_cb(f"✓ Chunking complete — {len(all_chunks)} chunks from {len(docs)} documents")
    return len(all_chunks)