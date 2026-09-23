"""
citation_format.py — One citation representation for every interface.

``build_citations(sources, answer)`` turns the verified source list returned by
``rag_pipeline.answer`` into user-safe citation records. The Streamlit app, the
REST API and the Mattermost bot all render these same records, so a citation
number, title and link can never differ between interfaces.

Each record::

    {n, document_id, title, url, source, source_label, access, content_type,
     content_type_label, authors, author_urls, date, publisher, section,
     excerpt, page_number}

``access`` is "public" for website content and "internal" for Commit KB / local
files (the URL is the authenticated Commit KB page; no credentials are ever
embedded). Retrieval internals (scores, chunk ids, embeddings) are omitted.
"""

from __future__ import annotations

import re
from typing import Dict, List

from src import config
from src.metadata import content_type_label

EXCERPT_CHARS = 300
_CITE_RE = re.compile(r"\[\s*Source\s*(\d+)\s*\]", re.IGNORECASE)
_TOK = re.compile(r"[a-z0-9]+")
PUBLIC_SOURCE_KEYS = {"website", "pdf"}


def _claims_for(answer: str, n: int) -> str:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", answer or ""):
        if any(int(m.group(1)) == n for m in _CITE_RE.finditer(sent)):
            out.append(_CITE_RE.sub("", sent))
    return " ".join(out)


def best_excerpt(text: str, claim: str, limit: int = EXCERPT_CHARS) -> str:
    """The passage window of ``text`` that best matches ``claim`` (or its start)."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    want = set(_TOK.findall((claim or "").lower()))
    if not want:
        return text[:limit].rsplit(" ", 1)[0] + "…"
    sentences = re.split(r"(?<=[.!?])\s+", text)
    best, best_score = 0, -1.0
    for i, s in enumerate(sentences):
        score = len(want & set(_TOK.findall(s.lower())))
        if score > best_score:
            best, best_score = i, score
    window = ""
    for s in sentences[best:]:
        if len(window) + len(s) > limit and window:
            break
        window = f"{window} {s}".strip()
    if len(window) > limit:
        window = window[:limit].rsplit(" ", 1)[0] + "…"
    return ("…" if best > 0 else "") + window


def build_citations(sources: List[Dict], answer: str = "") -> List[Dict]:
    out = []
    for n, s in enumerate(sources or [], start=1):
        source = (s.get("source") or "").lower()
        ctype = s.get("content_type") or s.get("document_type") or ""
        authors = s.get("authors") or ([s["author"]] if s.get("author") else [])
        out.append({
            "n": n,
            "document_id": s.get("document_id") or s.get("doc_id") or "",
            "title": (s.get("title") or "Untitled").strip(),
            "url": (s.get("url") or s.get("original_url") or "").strip(),
            "source": source,
            "source_label": s.get("source_name") or config.source_display_name(source, source),
            "access": "public" if source in PUBLIC_SOURCE_KEYS else "internal",
            "content_type": ctype,
            "content_type_label": content_type_label(ctype),
            "authors": list(authors),
            "author_urls": list(s.get("author_urls") or []),
            "date": s.get("publication_date") or s.get("date") or "",
            "publisher": s.get("publisher") or "",
            "section": s.get("heading_path") or s.get("section") or "",
            "page_number": s.get("page_number"),
            "excerpt": best_excerpt(s.get("text", ""), _claims_for(answer, n)),
        })
    return out
