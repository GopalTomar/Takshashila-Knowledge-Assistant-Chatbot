"""
metadata.py — The canonical document metadata schema and its normalisers.

Every document that enters the knowledge base — website page, PDF, Commit KB
note, local file — is passed through :func:`normalize_document`, which fills the
schema below **without inventing values**: a field is only populated from data
the source actually carried (HTML metadata, URL structure, the crawl itself).
Unknown stays empty. Legacy field names used elsewhere in the code base
(``doc_id``, ``source_type``, ``original_url``, ``author``, ``date``…) are kept as
mirrors so older modules continue to work.

Schema (all keys always present after normalisation)::

    document_id, source, source_priority, url, canonical_url, title, description,
    content (== text), content_type, section, category, subcategory, tags,
    author, author_url, authors (list[str]), author_urls (list[str]),
    publication_date, modified_date, crawl_date, parent_url, discovered_from,
    language, mime_type, http_status, content_hash, word_count, document_version,
    is_pdf, pdf_url, internal_links, external_links, breadcrumbs
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from src import config
from src.url_utils import canonicalize_url
from src.utils import content_hash as _content_hash

# ── Content types ────────────────────────────────────────────────────────────────
# Ordered (path-prefix → content_type) rules for takshashila.org.in. The first
# matching prefix wins. Derived from the live site's sitemap structure.
_WEBSITE_PATH_RULES = [
    ("/content/publications/", "publication"),
    ("/content/blogs/", "blog"),
    ("/content/team/", "person"),
    ("/content/trackers/", "tracker"),
    ("/pages/trackers/", "tracker"),
    ("/content/events/", "event"),
    ("/pages/events", "event"),
    ("/content/books/", "book"),
    ("/pages/books", "book"),
    ("/content/fellowships/", "programme"),
    ("/pages/fellowships", "programme"),
    ("/content/podcasts/", "podcast"),
    ("/pages/podcasts", "podcast"),
    ("/content/careers/", "career"),
    ("/pages/careers", "career"),
    ("/pages/policy-school/", "course"),
    ("/pages/research-areas/", "research_area"),
    ("/pages/newsletters", "newsletter"),
    ("/pages/news", "press"),
    ("/pages/team", "people_index"),
    ("/pages/publications", "listing"),
    ("/pages/blogs", "listing"),
    ("/pages/ai", "research_area"),
    ("/pages/contact", "contact"),
]
_ABOUT_PAGES = ("about", "mission", "heritage", "milestones", "donors", "ombudsman",
                "ai-policy", "privacy", "terms", "takshashila-library", "day-zero",
                "extraversity")

# Commit KB (Quarto) sections → content type.
_COMMIT_PATH_RULES = [
    ("/decisions", "decision"),
    ("/playbook", "playbook"),
    ("/insights", "insight"),
    ("/ideas", "idea"),
    ("/notes", "note"),
]

CONTENT_TYPE_LABELS = {
    "publication": "Publication", "blog": "Blog", "op-ed": "Op-ed",
    "person": "Team member", "tracker": "Tracker / Newsletter", "event": "Event",
    "book": "Book", "programme": "Programme", "podcast": "Podcast",
    "career": "Careers", "course": "Course", "research_area": "Research area",
    "newsletter": "Newsletter", "press": "Press / Media", "people_index": "Team",
    "listing": "Listing", "contact": "Contact", "about": "About", "pdf": "PDF",
    "page": "Page", "decision": "Decision", "playbook": "Playbook",
    "insight": "Insight", "idea": "Idea", "note": "Note", "kb": "Knowledge base",
    "holiday": "Holiday", "document": "Document",
}


def classify_content_type(url: str, source: str = "website",
                          categories: Iterable[str] = ()) -> str:
    """Deterministic content type from the URL structure (+ declared categories)."""
    path = (urlsplit(url).path if url else "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if source == "commit_kb":
        if path in ("", "/", "/index.html", "/browse.html"):
            return "listing"                     # KB home / browse-all index: titles only
        for prefix, ctype in _COMMIT_PATH_RULES:
            if path.startswith(prefix):
                return ctype
        return "kb"
    for prefix, ctype in _WEBSITE_PATH_RULES:
        if path.startswith(prefix):
            if ctype == "blog" and any(
                    c.strip().lower() in ("op-ed", "op-eds", "oped", "opinion")
                    for c in categories or ()):
                return "op-ed"
            return ctype
    slug = path.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
    if path.startswith("/pages/") and slug in _ABOUT_PAGES:
        return "about"
    if path in ("", "/"):
        return "listing"
    return "page"


def content_type_label(ctype: str) -> str:
    return CONTENT_TYPE_LABELS.get((ctype or "").lower(), (ctype or "Document").title())


# ── Dates ────────────────────────────────────────────────────────────────────────
_DATE_FORMATS = (
    "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d-%m-%Y",
    "%d/%m/%Y", "%B %Y", "%b %Y", "%Y/%m/%d",
)


def normalize_date(raw) -> str:
    """Return YYYY-MM-DD (or YYYY-MM / YYYY when that's all we know); '' if unparsable."""
    if not raw:
        return ""
    s = str(raw).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    s2 = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s2, fmt)
            if fmt in ("%B %Y", "%b %Y"):
                return dt.strftime("%Y-%m")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    if re.fullmatch(r"(19|20)\d{2}", s):
        return s
    return ""


# ── People / authors ─────────────────────────────────────────────────────────────
_NAME_TOKEN = r"[A-Z][A-Za-z.'À-ſ-]*"
_NAME_RE = re.compile(rf"^{_NAME_TOKEN}(?:\s+(?:{_NAME_TOKEN}|de|da|van|von|bin|al)){{1,4}}$")
_NOT_A_NAME = {
    "executive summary", "top", "context", "action items", "introduction", "abstract",
    "summary", "author", "authors", "date", "categories", "document", "version",
    "takshashila", "takshashila institution", "the takshashila institution",
    "staff", "admin", "editorial", "research", "read more", "share",
}


def looks_like_person_name(s: str) -> bool:
    s = (s or "").strip()
    if not (3 <= len(s) <= 60) or any(ch.isdigit() for ch in s):
        return False
    if s.lower() in _NOT_A_NAME:
        return False
    return bool(_NAME_RE.match(s))


def split_author_field(raw) -> List[str]:
    """
    Split a raw author value into candidate names. Handles lists, "A, B and C",
    "A & B", newline-polluted values produced by the legacy scraper
    (e.g. ``"Transformation\\nVanshika Saraf"``).
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        out: List[str] = []
        for r in raw:
            out.extend(split_author_field(r))
        return out
    parts = re.split(r"\n+|,|;|\s+&\s+|\s+and\s+|\|", str(raw))
    return [re.sub(r"\s+", " ", p).strip(" .:-–—") for p in parts if p and p.strip()]


def clean_authors(raw, known_people: Optional[Iterable[str]] = None) -> List[str]:
    """
    Return only the plausible person names from ``raw``. When ``known_people`` is
    given, names that exactly match a known team member are always kept, and
    anything else must still look like a person's name. Order is preserved.
    """
    known = {k.lower(): k for k in (known_people or [])}
    out: List[str] = []
    for cand in split_author_field(raw):
        canon = known.get(cand.lower())
        if canon:
            name = canon
        elif looks_like_person_name(cand):
            name = cand
        else:
            continue
        if name not in out:
            out.append(name)
    return out


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower(), flags=re.UNICODE)
    return re.sub(r"[\s_-]+", "-", s).strip("-")


# ── Normalisation ────────────────────────────────────────────────────────────────
def _as_list(v) -> List[str]:
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in re.split(r"[;,]", str(v)) if s.strip()]


def _relative_local_path(p: str) -> str:
    """Never persist absolute machine paths (they leak usernames/drive layout)."""
    if not p:
        return ""
    name = re.split(r"[\\/]", str(p))[-1]
    return f"data/raw/pdfs/{name}" if name.lower().endswith(".pdf") else ""


def _canon(u) -> str:
    """One spelling per URL (encoding, fragments, index.html) for http(s) URLs."""
    u = (u or "").strip()
    return (canonicalize_url(u) or u) if u.lower().startswith(("http://", "https://")) else u


def normalize_document(doc: Dict, known_people: Optional[Iterable[str]] = None) -> Dict:
    """
    Return a copy of ``doc`` conforming to the canonical schema. Values are only
    ever derived from what the document already carries — nothing is invented.
    """
    d = dict(doc)
    source = (d.get("source") or d.get("source_type") or "local").lower()
    # Legacy website PDFs were stored with source="pdf"; they are website content.
    if source == "pdf" and "takshashila.org.in" in (d.get("url") or ""):
        source = "website"
    url = _canon(d.get("url") or d.get("original_url") or "")
    text = d.get("text") or d.get("content") or ""

    categories = _as_list(d.get("categories")) or _as_list(d.get("category"))
    if source in ("website", "commit_kb") and url and not d.get("is_external_reference"):
        ctype = classify_content_type(url, source, categories)   # deterministic from URL
    else:
        ctype = d.get("content_type") or "document"
    if d.get("pdf_pages") or (d.get("source_type") == "pdf") or url.lower().endswith(".pdf"):
        is_pdf = True
    else:
        is_pdf = bool(d.get("is_pdf"))

    authors = clean_authors(d.get("authors") or d.get("author"), known_people)
    author_urls = _as_list(d.get("author_urls"))
    if not author_urls and d.get("author_url"):
        author_urls = [d["author_url"]]

    pub = normalize_date(d.get("publication_date") or d.get("date"))
    mod = normalize_date(d.get("modified_date") or d.get("updated_date"))

    title = (d.get("title") or "").strip() or "Untitled"
    description = (d.get("description") or d.get("subtitle") or "").strip()

    d.update({
        "document_id": d.get("document_id") or d.get("id") or d.get("doc_id") or "",
        "source": source,
        "source_priority": config.source_priority(source),
        "source_name": d.get("source_name") or config.source_display_name(source, source),
        "url": url,
        "canonical_url": _canon(d.get("canonical_url") or url),
        "title": title,
        "description": description,
        "text": text,
        "content_type": ctype,
        "section": d.get("section") or "",
        "category": d.get("category") if isinstance(d.get("category"), str) and d.get("category")
                    else (categories[0] if categories else ""),
        "categories": categories,
        "subcategory": d.get("subcategory") or "",
        "tags": _as_list(d.get("tags")) or categories,
        "authors": authors,
        "author": authors[0] if authors else "",
        "author_urls": author_urls,
        "author_url": author_urls[0] if author_urls else "",
        "publication_date": pub,
        "date": pub,
        "modified_date": mod,
        "updated_date": mod,
        "crawl_date": d.get("crawl_date") or d.get("scraped_at") or d.get("ingested_at") or "",
        "parent_url": d.get("parent_url") or (d.get("original_url") if is_pdf and d.get("original_url") != url else "") or "",
        "discovered_from": d.get("discovered_from") or "",
        "language": d.get("language") or "",
        "mime_type": d.get("mime_type") or ("application/pdf" if is_pdf else ("text/html" if url.startswith("http") else "")),
        "http_status": d.get("http_status") or (200 if url.startswith("http") else None),
        "content_hash": d.get("content_hash") or _content_hash(text),
        "word_count": len(text.split()),
        "document_version": d.get("document_version") or "",
        "is_pdf": is_pdf,
        "pdf_url": _canon(d.get("pdf_url") or (url if is_pdf else "")),
        "internal_links": d.get("internal_links") or [],
        "external_links": d.get("external_links") or [],
        "breadcrumbs": _as_list(d.get("breadcrumbs")),
        # Provenance: op-eds keep the outlet that published them; everything the
        # crawler fetched from Takshashila's own sites is first-party.
        "publisher": d.get("publisher") or (config.ORG_NAME if source in ("website", "commit_kb", "local")
                                            and not d.get("is_external_reference") else ""),
        # Audience: Commit KB is staff-only; the API/Mattermost scopes enforce it.
        "access": "internal" if source == "commit_kb" else "public",
        # legacy mirrors
        "doc_id": d.get("document_id") or d.get("id") or "",
        "original_url": _canon(d.get("original_url") or url),
    })
    d["source_type"] = "pdf" if is_pdf else (d.get("source_type") or ctype)
    if d.get("local_pdf_path"):
        d["local_pdf_path"] = _relative_local_path(d["local_pdf_path"])
    d.pop("content", None)   # ``text`` is the single copy of the body
    return d


SCHEMA_FIELDS = (
    "document_id", "source", "source_priority", "url", "canonical_url", "title",
    "description", "text", "content_type", "section", "category", "subcategory",
    "tags", "author", "author_url", "authors", "publication_date", "modified_date",
    "crawl_date", "parent_url", "discovered_from", "language", "mime_type",
    "http_status", "content_hash", "word_count", "document_version", "is_pdf",
    "pdf_url", "internal_links", "external_links", "breadcrumbs",
)
