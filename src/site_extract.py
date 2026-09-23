"""
site_extract.py — Structure-aware extraction for takshashila.org.in (Quarto).

The public site is a Quarto build with no JSON-LD and no <link rel=canonical>.
Its metadata lives in visible markup, so generic extractors (and the old
``[class*='author']`` selector fallback) produced polluted fields such as
``author = "Bharath Reddy\\n\\nExecutive Summary\\n\\nTop"``. This module reads the
markup the site actually uses:

* ``#title-block-header h1.title``          → title
* ``.gcpp-hero__lede``                      → subtitle (or a team member's role)
* the "Document Details" table              → AUTHOR (with profile links), DATE,
                                              DOCUMENT (series), VERSION, CATEGORIES
* ``meta[name=dcterms.date]``               → publication date fallback
* ``main#quarto-document-content``          → body, extracted block-by-block with
                                              Markdown headings kept so chunks stay
                                              section-aware
* team profiles (``/content/team/…``)       → role, biography, research areas and
                                              the person's listed publications, blogs
                                              and "In the news" op-eds
* listing pages (``/pages/news/``)          → one structured entry per listed item

Nothing is invented: every returned value is read from the page. Pure functions
(HTML in → dict out) so they are unit-testable without network access.
"""

from __future__ import annotations

import base64
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from src.metadata import clean_authors, normalize_date
from src.url_utils import canonicalize_url, is_pdf_url, same_site

_SITE_SUFFIX_RE = re.compile(r"\s*[–—|-]\s*Takshashila(?: Institution)?\s*$")
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre",
               "figcaption", "tr", "dt", "dd")
_DROP_SELECTORS = (
    "script", "style", "noscript", "svg", "nav", "footer", "form", "button",
    "#title-block-header", "#quarto-header", "#quarto-margin-sidebar",
    "#quarto-sidebar", ".quarto-listing", ".sidebar", ".tk-footer",
    ".listing-categories", ".quarto-title-meta", ".callout-subscribe",
    "[aria-hidden=true]", ".visually-hidden",
)


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _meta(soup: BeautifulSoup, name: str = "", prop: str = "") -> str:
    el = None
    if name:
        el = soup.find("meta", attrs={"name": name})
    if not el and prop:
        el = soup.find("meta", attrs={"property": prop})
    return (el.get("content") or "").strip() if el else ""


def _decode_categories(raw: str) -> List[str]:
    """Quarto encodes listing categories as base64(urlencoded 'A,B')."""
    if not raw:
        return []
    try:
        decoded = unquote(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace"))
    except Exception:
        return []
    return [c.strip() for c in decoded.split(",") if c.strip()]


# ── Header / Document Details ────────────────────────────────────────────────────
def _details_table(soup: BeautifulSoup, page_url: str) -> Dict:
    out: Dict = {"authors": [], "author_urls": [], "categories": []}
    header = soup.select_one("#title-block-header") or soup
    for row in header.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        key = _text(cells[0]).upper().rstrip(":")
        val = cells[1]
        if key.startswith("AUTHOR"):
            links = val.find_all("a", href=True)
            if links:
                for a in links:
                    name = _text(a)
                    if name and name not in out["authors"]:
                        out["authors"].append(name)
                        out["author_urls"].append(canonicalize_url(a["href"], page_url) or "")
            else:
                for name in clean_authors(_text(val)):
                    if name not in out["authors"]:
                        out["authors"].append(name)
                        out["author_urls"].append("")
        elif key == "DATE":
            out["date_raw"] = _text(val)
        elif key == "DOCUMENT":
            out["document_series"] = _text(val)
        elif key == "VERSION":
            out["document_version"] = _text(val)
        elif key.startswith("CATEGOR"):
            out["categories"] = [_text(a) for a in val.find_all("a")] or [
                c.strip() for c in _text(val).split(",") if c.strip()]
    if not out["categories"]:
        out["categories"] = [_text(a) for a in header.select("a.tk-tag") if _text(a)]
    return out


def _title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("#title-block-header h1") or soup.select_one("main h1") or soup.find("h1")
    t = _text(h1)
    if not t:
        t = _meta(soup, prop="og:title")
    if not t and soup.title:
        t = _text(soup.title)
    return _SITE_SUFFIX_RE.sub("", t).strip()


# ── Body text ─────────────────────────────────────────────────────────────────────
def _block_text(root: Tag) -> str:
    """Block-by-block text with Markdown headings; table rows joined by ' | '."""
    lines: List[str] = []
    seen_nested = set()
    for el in root.find_all(_BLOCK_TAGS):
        # Skip blocks nested inside another captured block (li inside li, p in li…).
        if any(id(p) in seen_nested for p in el.parents):
            continue
        if el.name == "tr":
            cells = [_text(c) for c in el.find_all(["td", "th"])]
            line = " | ".join(c for c in cells if c)
        else:
            line = _text(el)
        if not line:
            continue
        seen_nested.add(id(el))
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            line = "#" * int(el.name[1]) + " " + line
        elif el.name == "li":
            line = "- " + line
        lines.append(line)
    # Collapse exact consecutive duplicates (Quarto sometimes repeats headings).
    out: List[str] = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return "\n\n".join(out)


def _main_root(soup: BeautifulSoup) -> Tag:
    return (soup.select_one("main#quarto-document-content") or soup.select_one("main")
            or soup.select_one("article") or soup.body or soup)


def _clean_copy(root: Tag, keep_listings: bool = False) -> Tag:
    frag = BeautifulSoup(str(root), "lxml")
    for sel in _DROP_SELECTORS:
        if keep_listings and sel == ".quarto-listing":
            continue
        for el in frag.select(sel):
            el.decompose()
    return frag


def _links(root: Tag, page_url: str, site_domain: str) -> Tuple[List[str], List[str], List[str]]:
    internal, external, pdfs = [], [], []
    for a in root.find_all("a", href=True):
        u = canonicalize_url(a["href"], page_url)
        if not u:
            continue
        if same_site(u, site_domain):
            (pdfs if is_pdf_url(u) else internal).append(u)
        else:
            external.append(u)
    dedup = lambda xs: sorted(set(xs))  # noqa: E731
    return dedup(internal), dedup(external), dedup(pdfs)


def _figures(root: Tag, page_url: str) -> List[Dict]:
    figs = []
    for fig in root.find_all("figure"):
        img = fig.find("img")
        cap = _text(fig.find("figcaption"))
        if img or cap:
            figs.append({"src": canonicalize_url(img.get("src", ""), page_url) if img else "",
                         "alt": (img.get("alt") or "").strip() if img else "", "caption": cap})
    return figs[:30]


# ── Team profile ─────────────────────────────────────────────────────────────────
def _listing_items(container: Optional[Tag], page_url: str, kind: str) -> List[Dict]:
    items: List[Dict] = []
    if not container:
        return items
    # Card style (recent publications carousel): <a href><div.content-card><h2>
    for card in container.select(".content-card"):
        a = card.find_parent("a", href=True)
        title = _text(card.select_one(".content-title"))
        if title:
            items.append({"title": title, "kind": kind,
                          "url": canonicalize_url(a["href"], page_url) if a else "",
                          "authors": clean_authors(card.get("data-author-string", ""))})
    # Default listing style: .quarto-post
    for post in container.select(".quarto-post"):
        a = post.select_one(".listing-title a[href]")
        if not a:
            continue
        href = a["href"].strip()
        # Listing entries sometimes omit the scheme ("www.moneycontrol.com/…"); resolving
        # that relative to the listing page would fabricate a broken takshashila.org.in URL.
        if re.match(r"^www\.[a-z0-9-]+\.", href, re.I):
            href = "https://" + href
        items.append({
            "title": _text(a), "kind": kind,
            "url": canonicalize_url(href, page_url) or "",
            "date": normalize_date(_text(post.select_one(".listing-date"))),
            "authors": clean_authors(_text(post.select_one(".listing-authors, .listing-author"))),
            "outlet": _text(post.select_one(".listing-source")),
            "categories": _decode_categories(post.get("data-categories", "")),
        })
    return items


def _profile(soup: BeautifulSoup, page_url: str) -> Dict:
    main = _main_root(soup)
    role = _text(soup.select_one("#title-block-header .gcpp-hero__lede"))
    areas = [_text(a) for a in main.select(".research-categories a") if _text(a)]
    bio_parts: List[str] = []
    for p in main.find_all("p"):
        if p.find_parent(class_=re.compile(r"quarto-listing|research-categor|content-card")):
            continue
        t = _text(p)
        if not t or t.lower().startswith("areas of research"):
            continue
        bio_parts.append(t)
    works: List[Dict] = []
    for lid, kind in (("#listing-recent-pubs", "publication"),
                      ("#listing-recent-blogs", "blog"),
                      ("#listing-recent-news", "in_the_news")):
        works += _listing_items(main.select_one(lid), page_url, kind)
    return {"role": role, "bio": "\n\n".join(bio_parts), "research_areas": areas, "works": works}


def _profile_text(name: str, prof: Dict) -> str:
    """Readable body for a person document — only facts present on the page."""
    parts = [f"# {name}"]
    if prof["role"]:
        parts.append(f"Role: {prof['role']}")
    if prof["bio"]:
        parts.append(prof["bio"])
    if prof["research_areas"]:
        parts.append("## Areas of research\n\n" + "\n".join(f"- {a}" for a in prof["research_areas"]))
    labels = {"publication": "Publications listed on this profile",
              "blog": "Blog posts listed on this profile",
              "in_the_news": "In the news / op-eds listed on this profile"}
    for kind, label in labels.items():
        rows = [w for w in prof["works"] if w["kind"] == kind]
        if not rows:
            continue
        lines = []
        for w in rows:
            bits = [w["title"]]
            if w.get("outlet"):
                bits.append(w["outlet"])
            if w.get("date"):
                bits.append(w["date"])
            if w.get("authors") and w["authors"] != [name]:
                bits.append("with " + ", ".join(a for a in w["authors"] if a != name))
            lines.append("- " + " — ".join(b for b in bits if b))
        parts.append(f"## {label}\n\n" + "\n".join(lines))
    return "\n\n".join(parts)


# ── Public API ───────────────────────────────────────────────────────────────────
def extract_page(html: str, url: str, site_domain: str = "takshashila.org.in",
                 content_type: str = "") -> Dict:
    """
    Extract text + metadata from one site page. ``content_type`` (from the URL
    classifier) selects the profile extractor for team pages.
    """
    soup = BeautifulSoup(html, "lxml")
    title = _title(soup)
    details = _details_table(soup, url)
    root = _main_root(soup)
    internal, external, pdfs = _links(root, url, site_domain)
    # Header links (author profiles, category tags) are part of the page's graph too.
    hdr = soup.select_one("#title-block-header")
    if hdr:
        hi, he, hp = _links(hdr, url, site_domain)
        internal = sorted(set(internal) | set(hi))
        external = sorted(set(external) | set(he))
        pdfs = sorted(set(pdfs) | set(hp))

    date = normalize_date(details.get("date_raw")) or normalize_date(_meta(soup, name="dcterms.date"))
    description = _meta(soup, name="description") or _meta(soup, prop="og:description")
    html_el = soup.find("html")
    language = (html_el.get("lang") if html_el else "") or ""

    result: Dict = {
        "title": title,
        "subtitle": _text(soup.select_one("#title-block-header .gcpp-hero__lede")),
        "authors": details["authors"],
        "author_urls": details["author_urls"],
        "date": date,
        "categories": details["categories"],
        "document_series": details.get("document_series", ""),
        "document_version": details.get("document_version", ""),
        "description": description,
        "language": language,
        "internal_links": internal,
        "external_links": external,
        "pdf_links": pdfs,
        "figures": _figures(root, url),
        "breadcrumbs": [_text(li) for li in soup.select(".breadcrumb li, nav.quarto-page-breadcrumbs li") if _text(li)],
    }

    if content_type == "person":
        prof = _profile(soup, url)
        result.update({"role": prof["role"], "bio": prof["bio"],
                       "research_areas": prof["research_areas"], "works": prof["works"],
                       "text": _profile_text(title, prof)})
    else:
        result["text"] = _block_text(_clean_copy(root))
    return result


def extract_listing_entries(html: str, url: str, kind: str = "in_the_news") -> List[Dict]:
    """All ``.quarto-post`` entries on a listing page (e.g. /pages/news/)."""
    soup = BeautifulSoup(html, "lxml")
    return _listing_items(_main_root(soup), url, kind)
