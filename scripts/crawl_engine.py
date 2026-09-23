"""
crawl_engine.py — Robust, incremental, auditable crawler for both KB sources.

One engine crawls the public website (takshashila.org.in) and the authenticated
Commit KB and emits documents in the project schema (src/metadata.py).

Discovery
  * robots.txt (website), sitemap.xml + sitemap indexes (recursive, with lastmod),
    configured listing seeds, every previously-known URL, and BFS over internal
    links. URLs are canonicalised (src/url_utils.py) so each page has one identity.

Fetching
  * One shared rate limiter (SCRAPE_MAX_RPS) across the thread pool.
  * Manual retries with exponential backoff + jitter for timeouts, connection
    errors, 429 and 5xx (Retry-After honoured); the retry count is recorded.
  * Conditional GET (ETag / Last-Modified) → 304 means no download at all.
  * Redirect chains and loops are recorded; a redirected page is stored under its
    final URL and the old document is retired as "redirected".

Change detection (content hash is authoritative)
  * page: sha of extracted text + key metadata (title/authors/date/categories).
  * PDF: sha of the raw bytes first (skip extraction when identical), then text.
  * op-ed entries on /pages/news/: one document per entry, hashed individually.

Safety
  * Temporary failures (timeouts, 5xx, 401/403 on a sub-page) keep the previous
    document and mark it unavailable.
  * A failed entry point (base URL unreachable / auth failure) aborts the crawl
    for that source: no documents change and nothing is removed.
  * Removals require a COMPLETE crawl, a failure ratio under KB_MAX_FAILURE_RATIO
    and KB_REMOVAL_CONFIRMATIONS consecutive misses (404/410 or unlinked).

Audit trail
  * Every URL touched gets a record (status, final URL, redirect chain, error,
    retries, discovered_from, discovery methods, inlinks, content type, outcome).
    ``CrawlResult.log`` feeds scripts/audit_website.py and the refresh reports.
"""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
import threading
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from src import config
from src.crawl_state import CrawlState
from src.metadata import classify_content_type, clean_authors, normalize_date, normalize_document
from src.url_utils import (
    canonicalize_url, is_asset_url, is_malformed_href, is_pdf_url, same_site,
)
from src.utils import (
    clean_document_metadata, clean_text, content_hash, get_logger, is_listing_or_landing,
    now_iso, url_hash,
)

logger = get_logger("crawl_engine", config.SCRAPE_LOG)


class CrawlAborted(RuntimeError):
    """The source's entry point failed (unreachable / authentication) — nothing changed."""


# ════════════════════════════════════════════════════════════════════════════════
#  Site configuration
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class SiteConfig:
    base_url: str
    domain: str
    source: str                       # "website" | "commit_kb"
    source_name: str
    sitemap_urls: List[str] = field(default_factory=list)
    listing_urls: List[Tuple[str, str]] = field(default_factory=list)
    include_patterns: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)
    follow_exclude_patterns: List[str] = field(default_factory=list)
    doc_exclude_patterns: List[str] = field(default_factory=list)
    oped_listing_paths: List[str] = field(default_factory=list)
    extra_seeds: List[str] = field(default_factory=list)   # e.g. legacy document URLs
    skip_extensions: Tuple[str, ...] = ()
    min_text_len: int = 200
    max_pages: int = 5000
    max_depth: int = 8
    include_pdfs: bool = True
    respect_robots: bool = True
    username: str = ""
    password: str = ""
    max_workers: int = 8

    def __post_init__(self):
        if not self.follow_exclude_patterns:
            self.follow_exclude_patterns = list(self.exclude_patterns)
        if not self.doc_exclude_patterns:
            self.doc_exclude_patterns = list(self.exclude_patterns)


def website_config(max_pages: Optional[int] = None, max_depth: Optional[int] = None,
                   include_pdfs: Optional[bool] = None) -> SiteConfig:
    return SiteConfig(
        base_url=config.WEBSITE_BASE_URL,
        domain=config.WEBSITE_DOMAIN,
        source="website",
        source_name="Takshashila Website",
        sitemap_urls=list(config.WEBSITE_SITEMAP_URLS),
        listing_urls=list(config.WEBSITE_LISTING_URLS),
        follow_exclude_patterns=list(config.WEBSITE_FOLLOW_EXCLUDE_PATTERNS),
        doc_exclude_patterns=list(config.WEBSITE_DOC_EXCLUDE_PATTERNS),
        oped_listing_paths=list(config.WEBSITE_OPED_LISTING_PATHS),
        skip_extensions=tuple(config.WEBSITE_SKIP_EXTENSIONS),
        min_text_len=config.WEBSITE_MIN_TEXT_LEN,
        max_pages=config.WEBSITE_MAX_PAGES if max_pages is None else max_pages,
        max_depth=config.WEBSITE_MAX_DEPTH if max_depth is None else max_depth,
        include_pdfs=config.WEBSITE_INCLUDE_PDFS if include_pdfs is None else include_pdfs,
        respect_robots=True,
        max_workers=config.SCRAPE_MAX_WORKERS,
    )


def commit_kb_config(max_pages: Optional[int] = None, max_depth: Optional[int] = None,
                     include_pdfs: Optional[bool] = None) -> SiteConfig:
    base = config.COMMIT_KB_URL.rstrip("/") + "/"
    return SiteConfig(
        base_url=base,
        domain=urlsplit(base).netloc,
        source="commit_kb",
        source_name="Commit KB",
        sitemap_urls=[urljoin(base, "sitemap.xml"), urljoin(base, "sitemap_index.xml")],
        follow_exclude_patterns=["/wp-admin/", "/wp-login", "/login", "/logout",
                                 "?share=", "?replytocom=", "/feed/"],
        doc_exclude_patterns=[],        # every KB page is kept (short pages carry summaries)
        skip_extensions=tuple(config.WEBSITE_SKIP_EXTENSIONS),
        min_text_len=60,
        max_pages=config.WEBSITE_MAX_PAGES if max_pages is None else max_pages,
        max_depth=8 if max_depth is None else max_depth,
        include_pdfs=config.WEBSITE_INCLUDE_PDFS if include_pdfs is None else include_pdfs,
        respect_robots=False,
        username=config.COMMIT_KB_USERNAME,
        password=config.COMMIT_KB_PASSWORD,
        max_workers=config.SCRAPE_MAX_WORKERS,
    )


# ════════════════════════════════════════════════════════════════════════════════
#  Results
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class CrawlResult:
    docs: List[Dict] = field(default_factory=list)          # new + changed documents
    removed_ids: List[str] = field(default_factory=list)     # documents to retire
    counts: Dict[str, int] = field(default_factory=dict)
    discovered: int = 0
    changes: List[Dict] = field(default_factory=list)        # per-document change log
    log: Dict[str, Dict] = field(default_factory=dict)       # per-URL audit records
    status: str = "success"                                  # success | partial | aborted
    error: str = ""
    complete: bool = True


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int] = None
    final_url: str = ""
    redirect_chain: List[Tuple[str, int]] = field(default_factory=list)
    content_type: str = ""
    body: bytes = b""
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    error: str = ""
    error_type: str = ""          # timeout | dns | connection | redirect_loop | malformed | http
    retries: int = 0
    elapsed_ms: int = 0


class RateLimiter:
    """Global request pacing shared by all worker threads."""

    def __init__(self, max_rps: float):
        self.interval = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            time.sleep(wait)


_RETRY_STATUS = {429, 500, 502, 503, 504}


# ════════════════════════════════════════════════════════════════════════════════
#  Engine
# ════════════════════════════════════════════════════════════════════════════════

class CrawlEngine:
    def __init__(self, site: SiteConfig, state: CrawlState, incremental: bool = True,
                 progress_cb: Optional[Callable] = None, session: Optional[requests.Session] = None,
                 audit_only: bool = False):
        self.site = site
        self.state = state
        self.incremental = incremental
        self.audit_only = audit_only          # fetch + log everything, produce no documents
        self.progress_cb = progress_cb
        self.session = session or self._build_session()
        self.rate = RateLimiter(config.SCRAPE_MAX_RPS)
        self.robots = None
        self.base_canonical = canonicalize_url(site.base_url) or site.base_url

        self.frontier: deque = deque()
        self.depth: Dict[str, int] = {}
        self.enqueued: Set[str] = set()
        self.processed_urls: Set[str] = set()
        self.log: Dict[str, Dict] = {}
        self.sitemap_lastmod: Dict[str, str] = {}
        self.result = CrawlResult()
        self._counts = {k: 0 for k in (
            "added", "updated", "unchanged", "failed", "not_modified", "redirected", "gone",
            "pdf_added", "pdf_updated", "pdf_unchanged", "oped_added", "oped_updated",
            "oped_unchanged", "skipped_nav", "skipped_short", "not_html", "robots_blocked",
            "removed", "removal_pending")}
        self._lock = threading.RLock()
        self._pdf_queue: Dict[str, Dict] = {}     # pdf url -> parent metadata
        self._replaced_ids: Set[str] = set()       # old ids of documents that got a new id

    # ── small helpers ────────────────────────────────────────────────────────────
    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + n

    def _say(self, msg: str) -> None:
        if self.progress_cb:
            self.progress_cb(msg)

    def _rec(self, url: str) -> Dict:
        with self._lock:
            if url not in self.log:
                self.log[url] = {"url": url, "discovered_from": "", "discovery": [],
                                 "inlinks": [], "depth": None}
            return self.log[url]

    def _note_discovery(self, url: str, method: str, referrer: str = "") -> None:
        with self._lock:
            r = self._rec(url)
            if method not in r["discovery"]:
                r["discovery"].append(method)
            if referrer:
                if not r["discovered_from"]:
                    r["discovered_from"] = referrer
                if referrer not in r["inlinks"] and len(r["inlinks"]) < 200:
                    r["inlinks"].append(referrer)

    def _change(self, kind: str, url: str, doc: Optional[Dict] = None, **extra) -> None:
        with self._lock:
            self.result.changes.append({
                "change": kind, "url": url, "source": self.site.source,
                "document_id": (doc or {}).get("document_id") or self.state.document_id(url) or "",
                "content_type": (doc or {}).get("content_type") or extra.pop("content_type", ""),
                "title": (doc or {}).get("title", ""), **extra})

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=0, pool_maxsize=32)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({"User-Agent": config.USER_AGENT})
        if self.site.username:
            s.auth = (self.site.username, self.site.password)
        return s

    # ── fetching ─────────────────────────────────────────────────────────────────
    def fetch(self, url: str, conditional: bool = True) -> FetchResult:
        headers = self.state.conditional_headers(url) if (conditional and self.incremental) else {}
        fr = FetchResult(url=url)
        t0 = time.perf_counter()
        max_retries = max(0, config.SCRAPE_MAX_RETRIES)
        for attempt in range(max_retries + 1):
            fr.retries = attempt
            self.rate.acquire()
            try:
                r = self.session.get(url, headers=headers, timeout=config.SCRAPE_TIMEOUT,
                                     allow_redirects=True)
            except requests.TooManyRedirects as exc:
                fr.error, fr.error_type = str(exc)[:300], "redirect_loop"
                break
            except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                    requests.exceptions.InvalidSchema) as exc:
                fr.error, fr.error_type = str(exc)[:300], "malformed"
                break
            except requests.Timeout as exc:
                fr.error, fr.error_type = str(exc)[:300], "timeout"
            except requests.ConnectionError as exc:
                msg = str(exc)
                dns = any(s in msg for s in ("NameResolution", "getaddrinfo", "Name or service"))
                fr.error, fr.error_type = msg[:300], "dns" if dns else "connection"
            except requests.RequestException as exc:
                fr.error, fr.error_type = str(exc)[:300], "connection"
            else:
                fr.status_code = r.status_code
                fr.final_url = canonicalize_url(r.url) or r.url
                fr.redirect_chain = [(canonicalize_url(h.url) or h.url, h.status_code) for h in r.history]
                fr.content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                fr.headers = {k: v for k, v in r.headers.items()
                              if k.lower() in ("etag", "last-modified", "retry-after", "content-length")}
                if r.status_code in _RETRY_STATUS and attempt < max_retries:
                    delay = self._backoff(attempt, r.headers.get("Retry-After"))
                    time.sleep(delay)
                    continue
                fr.error, fr.error_type = "", ""
                if r.status_code >= 400:
                    fr.error_type = "http"
                    fr.error = f"HTTP {r.status_code}"
                if r.status_code == 200:
                    fr.body = r.content
                    if fr.content_type in ("text/html", "application/xhtml+xml", "") and not is_pdf_url(fr.final_url):
                        r.encoding = r.encoding if (r.encoding and r.encoding.lower() != "iso-8859-1") else "utf-8"
                        fr.text = r.text
                break
            if fr.error_type in ("dns",) and attempt >= 1:
                break
            if attempt < max_retries:
                time.sleep(self._backoff(attempt))
        fr.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return fr

    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[str] = None) -> float:
        if retry_after:
            try:
                return min(60.0, float(retry_after))
            except ValueError:
                pass
        return min(30.0, (2 ** attempt) * max(0.5, config.SCRAPE_DELAY)) + random.uniform(0, 0.5)

    # ── robots / sitemap ─────────────────────────────────────────────────────────
    def _load_robots(self) -> None:
        if not self.site.respect_robots:
            return
        robots_url = urljoin(self.site.base_url, "/robots.txt")
        fr = self.fetch(robots_url, conditional=False)
        if fr.status_code == 200 and fr.body:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(fr.body.decode("utf-8", "replace").splitlines())
            self.robots = rp
            for line in fr.body.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("sitemap:"):
                    sm = line.split(":", 1)[1].strip()
                    if sm and sm not in self.site.sitemap_urls:
                        self.site.sitemap_urls.insert(0, sm)

    def _can_fetch(self, url: str) -> bool:
        if not self.robots:
            return True
        try:
            return self.robots.can_fetch(config.USER_AGENT, url)
        except Exception:
            return True

    def discover_sitemap(self) -> List[str]:
        found: List[str] = []
        visited: Set[str] = set()
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        def parse(url: str, depth: int = 0) -> None:
            if depth > 5 or url in visited:
                return
            visited.add(url)
            fr = self.fetch(url, conditional=False)
            if fr.status_code != 200 or not fr.body:
                if fr.status_code not in (None, 404):
                    logger.warning(f"[{self.site.source}] sitemap {url}: {fr.error or fr.status_code}")
                return
            try:
                root = ET.fromstring(fr.body)
            except ET.ParseError as exc:
                logger.warning(f"[{self.site.source}] sitemap parse error {url}: {exc}")
                return
            for sm in root.findall(".//sm:sitemap", ns):
                loc = sm.find("sm:loc", ns)
                if loc is not None and loc.text:
                    parse(loc.text.strip(), depth + 1)
            for u in root.findall(".//sm:url", ns):
                loc = u.find("sm:loc", ns)
                if loc is None or not loc.text:
                    continue
                cu = canonicalize_url(loc.text.strip())
                if not cu or not same_site(cu, self.site.domain):
                    continue
                lm = u.find("sm:lastmod", ns)
                if lm is not None and lm.text:
                    self.sitemap_lastmod[cu] = lm.text.strip()
                found.append(cu)

        for sm in list(dict.fromkeys(self.site.sitemap_urls)):
            parse(sm)
        found = list(dict.fromkeys(found))
        logger.info(f"[{self.site.source}] sitemap discovered {len(found)} URLs")
        return found

    # ── link rules ───────────────────────────────────────────────────────────────
    def _should_follow(self, url: str) -> bool:
        if not same_site(url, self.site.domain):
            return False
        if is_asset_url(url) or is_pdf_url(url):
            return False
        low = url.lower()
        return not any(p in low for p in self.site.follow_exclude_patterns)

    def _should_keep_doc(self, url: str, title: str = "") -> bool:
        low = url.lower()
        if any(p in low for p in self.site.doc_exclude_patterns):
            return False
        if self.site.source != "commit_kb" and is_listing_or_landing(url, title):
            return False
        if self.site.include_patterns and not any(p in low for p in self.site.include_patterns):
            return False
        return True

    def _is_oped_listing(self, url: str) -> bool:
        path = urlsplit(url).path
        return any(path.rstrip("/") == p.rstrip("/") for p in self.site.oped_listing_paths)

    # ── document building ────────────────────────────────────────────────────────
    @staticmethod
    def _doc_hash(doc: Dict) -> str:
        key = json.dumps({"t": doc.get("text", ""), "title": doc.get("title"),
                          "a": doc.get("authors"), "d": doc.get("date"),
                          "c": doc.get("categories"), "desc": doc.get("description")},
                         ensure_ascii=False, sort_keys=True)
        return content_hash(key)

    def _fallback_text(self, html: str, url: str) -> str:
        try:
            import trafilatura
            text = trafilatura.extract(html, include_tables=True, include_links=False,
                                       favor_recall=True, url=url) or ""
        except Exception:
            text = ""
        if len(text.strip()) < 40:
            soup = BeautifulSoup(html, "lxml")
            el = soup.select_one("main") or soup.select_one("article") or soup.body
            text = el.get_text("\n", strip=True) if el else ""
        return clean_text(text)

    def build_doc(self, url: str, html: str, fetch: Optional[FetchResult] = None,
                  discovered_from: str = "") -> Tuple[Optional[Dict], Dict, str]:
        """Return (document or None, extraction dict, reason)."""
        from src.site_extract import extract_page
        ctype = classify_content_type(url, self.site.source)
        ex = extract_page(html, url, site_domain=self.site.domain, content_type=ctype)
        title = ex.get("title") or ""
        if not self._should_keep_doc(url, title):
            return None, ex, "navigation_page"
        text = ex.get("text") or ""
        text = "\n".join(ln for ln in text.split("\n") if ln.strip().lower() not in ("download document",))
        if len(text) < self.site.min_text_len:
            fb = self._fallback_text(html, url)
            if len(fb) > len(text):
                text = fb
        if len(text) < self.site.min_text_len and ctype not in ("person",):
            return None, ex, "too_short"

        authors = ex.get("authors") or []
        if not authors:
            # Byline-less pages: accept an explicit <meta name=author> only.
            m = BeautifulSoup(html, "lxml").find("meta", attrs={"name": "author"})
            authors = clean_authors(m.get("content", "") if m else "")
        categories = ex.get("categories") or []
        ctype = classify_content_type(url, self.site.source, categories)
        uid = url_hash(url)
        doc = {
            "document_id": f"{self.site.source}_{uid}",
            "url_hash": uid, "page_id": uid,
            "url": url, "original_url": url,
            "canonical_url": url,
            "title": title or url,
            "subtitle": ex.get("subtitle", ""),
            "description": ex.get("description", ""),
            "authors": authors,
            "author_urls": ex.get("author_urls") or [],
            "date": ex.get("date", ""),
            "modified_date": normalize_date(self.sitemap_lastmod.get(url, "")),
            "categories": categories,
            "category": categories[0] if categories else "",
            "tags": categories,
            "document_series": ex.get("document_series", ""),
            "subcategory": ex.get("document_series", ""),
            "document_version": ex.get("document_version", ""),
            "language": ex.get("language", ""),
            "breadcrumbs": ex.get("breadcrumbs", []),
            "content_type": ctype,
            "source": self.site.source,
            "source_name": self.site.source_name,
            "source_type": ctype,
            "text": text,
            "internal_links": ex.get("internal_links", [])[:300],
            "external_links": ex.get("external_links", [])[:300],
            "pdf_urls": ex.get("pdf_links", []),
            "pdf_url": (ex.get("pdf_links") or [""])[0],
            "figures": ex.get("figures", []),
            "discovered_from": discovered_from,
            "http_status": fetch.status_code if fetch else 200,
            "mime_type": "text/html",
            "crawl_date": now_iso(),
            "extraction_method": "quarto-structured",
        }
        for k in ("role", "bio", "research_areas", "works"):
            if k in ex:
                doc[k] = ex[k]
        doc = clean_document_metadata(doc)
        doc["content_hash"] = self._doc_hash(doc)
        return normalize_document(doc), ex, "kept"

    # ── PDFs ─────────────────────────────────────────────────────────────────────
    def _queue_pdf(self, pdf_url: str, parent: Dict) -> None:
        with self._lock:
            # Prefer a real parent page (title/authors/date) over a bare sitemap/seed hit.
            if pdf_url not in self._pdf_queue or (not self._pdf_queue[pdf_url].get("title")
                                                  and parent.get("title")):
                self._pdf_queue[pdf_url] = parent

    def _retire_if_replaced(self, url: str, new_id: str) -> None:
        """Same URL, new document id (e.g. identity scheme changed): retire the old id."""
        old = self.state.document_id(url)
        if old and new_id and old != new_id:
            with self._lock:
                self._replaced_ids.add(old)

    def process_pdf(self, pdf_url: str, parent: Dict) -> Optional[Dict]:
        rec = self._rec(pdf_url)
        rec["kind"] = "pdf"
        fr = self.fetch(pdf_url)
        rec.update(status=fr.status_code, error=fr.error, error_type=fr.error_type,
                   retries=fr.retries, content_type=fr.content_type, fetched_at=now_iso(),
                   elapsed_ms=fr.elapsed_ms, final_url=fr.final_url,
                   redirect_chain=fr.redirect_chain)
        if fr.status_code == 304:
            self.state.mark_seen(pdf_url)
            self._bump("pdf_unchanged")
            rec["outcome"] = "not_modified"
            return None
        if fr.status_code in (404, 410):
            self._bump("gone")
            rec["outcome"] = "gone"
            return None
        if fr.status_code != 200 or not fr.body:
            self.state.mark_unavailable(pdf_url, fr.status_code or fr.error_type)
            self._bump("failed")
            rec["outcome"] = "failed"
            self._change("failed", pdf_url, content_type="pdf", error=fr.error or str(fr.status_code))
            return None
        bytes_hash = hashlib.sha256(fr.body).hexdigest()[:24]
        prev = self.state.get(pdf_url)
        if self.incremental and prev.get("bytes_hash") == bytes_hash:
            self.state.record(pdf_url, content_hash=prev.get("content_hash", ""),
                              document_id=prev.get("document_id", ""),
                              etag=fr.headers.get("ETag", ""),
                              last_modified=fr.headers.get("Last-Modified", ""), changed=False)
            self._bump("pdf_unchanged")
            rec["outcome"] = "unchanged"
            return None
        if self.audit_only:
            rec["outcome"] = "fetched"
            return None

        from src.extractors import extract_pdf_metadata, extract_pdf_text
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "doc.pdf"
            path.write_bytes(fr.body)
            pages = extract_pdf_text(path)
            pmeta = extract_pdf_metadata(path)
        text = "\n\n".join(p["text"] for p in pages)
        rec["text_len"] = len(text)
        if not text.strip():
            rec["outcome"] = "no_text"
            self._change("rejected", pdf_url, content_type="pdf", reason="PDF has no extractable text")
            self.state.record(pdf_url, content_hash="", document_id="", changed=False, bytes_hash=bytes_hash)
            return None
        uid = url_hash(pdf_url)
        parent_title = parent.get("title") or ""
        title = (f"{parent_title} [PDF]" if parent_title else "") or pmeta.get("pdf_title") \
            or Path(urlsplit(pdf_url).path).stem.replace("-", " ")
        doc = {
            "document_id": f"{self.site.source}_pdf_{uid}",
            "url_hash": uid, "url": pdf_url, "original_url": parent.get("url", pdf_url),
            "canonical_url": pdf_url, "pdf_url": pdf_url, "parent_url": parent.get("url", ""),
            "title": title,
            "authors": parent.get("authors") or clean_authors(pmeta.get("pdf_author", "")),
            "author_urls": parent.get("author_urls") or [],
            "date": parent.get("date") or pmeta.get("pdf_creation_date", ""),
            "modified_date": pmeta.get("pdf_modified_date", ""),
            "categories": parent.get("categories") or [],
            "category": parent.get("category", "") or "",
            "document_series": parent.get("document_series", ""),
            "description": parent.get("description", ""),
            "source": self.site.source, "source_name": self.site.source_name,
            "source_type": "pdf", "content_type": "pdf", "is_pdf": True,
            "text": text, "pdf_pages": pages, "mime_type": "application/pdf",
            "http_status": 200, "discovered_from": parent.get("url", ""),
            "crawl_date": now_iso(), "extraction_method": "pymupdf", **pmeta,
        }
        doc = clean_document_metadata(doc)
        doc["content_hash"] = content_hash(text)
        doc = normalize_document(doc)
        changed = self.state.known(pdf_url) and bool(prev.get("content_hash"))
        if self.incremental and prev.get("content_hash") == doc["content_hash"]:
            self.state.record(pdf_url, content_hash=doc["content_hash"],
                              document_id=prev.get("document_id") or doc["document_id"],
                              etag=fr.headers.get("ETag", ""), last_modified=fr.headers.get("Last-Modified", ""),
                              changed=False, bytes_hash=bytes_hash)
            self._bump("pdf_unchanged")
            rec["outcome"] = "unchanged"
            return None
        self._retire_if_replaced(pdf_url, doc["document_id"])
        self.state.record(pdf_url, content_hash=doc["content_hash"], document_id=doc["document_id"],
                          etag=fr.headers.get("ETag", ""), last_modified=fr.headers.get("Last-Modified", ""),
                          changed=True, bytes_hash=bytes_hash, parent=parent.get("url", ""))
        self._bump("pdf_updated" if changed else "pdf_added")
        rec["outcome"] = "modified" if changed else "new"
        self._change("modified" if changed else "new", pdf_url, doc)
        return doc

    # ── op-ed entries (/pages/news/) ─────────────────────────────────────────────
    def process_oped_listing(self, url: str, html: str) -> Tuple[List[Dict], List[str]]:
        from src.site_extract import extract_listing_entries
        entries = extract_listing_entries(html, url, kind="in_the_news")
        docs: List[Dict] = []
        children = []
        for e in entries:
            eu = e.get("url")
            if not eu or not e.get("title"):
                continue
            children.append(eu)
            lines = [f"Op-ed / media article by Takshashila authors: {e['title']}"]
            if e.get("authors"):
                lines.append("Authors: " + ", ".join(e["authors"]))
            if e.get("outlet"):
                lines.append(f"Published in: {e['outlet']}")
            if e.get("date"):
                lines.append(f"Date: {e['date']}")
            if e.get("categories"):
                lines.append("Topics: " + ", ".join(e["categories"]))
            lines.append(f"Listed on the Takshashila website's 'In the news' page: {url}")
            text = "\n".join(lines)
            uid = url_hash(eu)
            doc = normalize_document({
                "document_id": f"{self.site.source}_oped_{uid}", "url": eu, "canonical_url": eu,
                "title": e["title"], "authors": e.get("authors") or [], "date": e.get("date", ""),
                "categories": e.get("categories") or [], "category": (e.get("categories") or [""])[0],
                "tags": e.get("categories") or [], "publisher": e.get("outlet", ""),
                "source": self.site.source, "source_name": self.site.source_name,
                "content_type": "op-ed", "source_type": "op-ed", "text": text,
                "parent_url": url, "discovered_from": url, "is_external_reference": True,
                "mime_type": "text/html", "crawl_date": now_iso(),
                "extraction_method": "listing-entry",
            })
            doc["content_type"] = "op-ed"          # external URL → classifier can't know
            doc["content_hash"] = content_hash(f"{text}\n{eu}")   # a changed link is a change
            prev = self.state.get(eu)
            if self.incremental and prev.get("content_hash") == doc["content_hash"]:
                self.state.mark_seen(eu)
                self._bump("oped_unchanged")
                continue
            changed = bool(prev)
            self._retire_if_replaced(eu, doc["document_id"])
            self.state.record(eu, content_hash=doc["content_hash"], document_id=doc["document_id"],
                              changed=True, parent=url)
            self._bump("oped_updated" if changed else "oped_added")
            self._change("modified" if changed else "new", eu, doc)
            docs.append(doc)
        return docs, children

    # ── per-page processing ──────────────────────────────────────────────────────
    def process(self, url: str) -> Tuple[List[Dict], List[str]]:
        """Fetch + process one page. Returns (documents, internal links to follow)."""
        rec = self._rec(url)
        rec["kind"] = "page"
        if not self._can_fetch(url):
            self.state.mark_seen(url)
            self._bump("robots_blocked")
            rec["outcome"] = "robots_blocked"
            return [], []

        fr = self.fetch(url)
        rec.update(status=fr.status_code, final_url=fr.final_url, redirect_chain=fr.redirect_chain,
                   error=fr.error, error_type=fr.error_type, retries=fr.retries,
                   content_type=fr.content_type, fetched_at=now_iso(), elapsed_ms=fr.elapsed_ms,
                   sitemap_lastmod=self.sitemap_lastmod.get(url, ""))

        if fr.status_code == 304:
            self.state.mark_seen(url)
            self._bump("not_modified")
            self._bump("unchanged")
            rec["outcome"] = "not_modified"
            prev = self.state.get(url)
            outlinks = [canonicalize_url(u) or u for u in prev.get("outlinks") or []]
            rec["outlinks"] = outlinks
            # The page is unchanged, but its PDFs may not be: re-check them (a 304 or
            # byte-hash match is cheap) using the parent metadata stored last time.
            if self.site.include_pdfs:
                meta = {"url": url, **(prev.get("meta") or {})}
                for c in prev.get("children") or []:
                    cu = canonicalize_url(c) or c
                    if is_pdf_url(cu) and same_site(cu, self.site.domain):
                        self._note_discovery(cu, "links", url)
                        self._queue_pdf(cu, meta)
            return [], outlinks
        if fr.status_code in (404, 410):
            self._bump("gone")
            rec["outcome"] = "gone"
            if self.state.known(url):
                self._change("missing", url, status=fr.status_code)
            return [], []
        if fr.status_code is None or fr.status_code >= 400 or fr.status_code != 200:
            # Temporary / access failure: keep the previous document.
            self.state.mark_unavailable(url, fr.status_code or fr.error_type)
            self._bump("failed")
            rec["outcome"] = "failed"
            self._change("failed", url, error=fr.error or f"HTTP {fr.status_code}")
            return [], []

        final = fr.final_url or url
        if final != url:
            self._bump("redirected")
            rec["outcome_redirect"] = final
            if not same_site(final, self.site.domain):
                self.state.mark_seen(url)
                rec["outcome"] = "external_redirect"
                return [], []
            with self._lock:
                already = final in self.processed_urls
                self.processed_urls.add(final)
            self._rec(final)
            self._note_discovery(final, "redirect", url)
            old_id = self.state.document_id(url)
            self.state.update_fields(url, redirected_to=final)
            if old_id:
                self._change("redirected", url, to=final, document_id=old_id)
            if already:
                rec["outcome"] = "redirect_duplicate"
                return [], []
            url = final

        if fr.content_type == "application/pdf" or is_pdf_url(url):
            self._queue_pdf(url, {"url": rec.get("discovered_from", "")})
            rec["outcome"] = "pdf_queued"
            return [], []
        if fr.content_type not in ("text/html", "application/xhtml+xml", ""):
            self.state.mark_seen(url)
            self._bump("not_html")
            rec["outcome"] = "not_html"
            return [], []

        html = fr.text
        soup = BeautifulSoup(html, "lxml")
        links, malformed = [], []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if is_malformed_href(href):
                malformed.append(href)
                continue
            cu = canonicalize_url(href, url)
            if cu and same_site(cu, self.site.domain):
                links.append(cu)
        links = sorted(set(links))
        rec["outlinks"] = links
        if malformed:
            rec["malformed_links"] = malformed[:50]
        page_links = [l for l in links if not is_pdf_url(l)]
        pdf_links = [l for l in links if is_pdf_url(l)]

        docs: List[Dict] = []
        doc, ex, reason = (None, {}, "audit") if self.audit_only else self.build_doc(
            url, html, fr, rec.get("discovered_from", ""))
        if self.audit_only:
            from src.site_extract import extract_page
            ex = extract_page(html, url, site_domain=self.site.domain,
                              content_type=classify_content_type(url, self.site.source))
        rec["title"] = ex.get("title", "")
        rec["text_len"] = len(ex.get("text") or "")
        rec["has_author"] = bool(ex.get("authors"))
        rec["has_date"] = bool(ex.get("date"))
        rec["text_hash"] = content_hash(ex.get("text") or "")

        # PDFs linked from this page (always checked; unchanged ones cost a 304/byte-hash).
        parent_meta = doc or {"url": url, "title": ex.get("title", ""), "authors": ex.get("authors", []),
                              "author_urls": ex.get("author_urls", []), "date": ex.get("date", ""),
                              "categories": ex.get("categories", []),
                              "document_series": ex.get("document_series", "")}
        if self.site.include_pdfs:
            for p in pdf_links:
                self._note_discovery(p, "links", url)
                self._queue_pdf(p, parent_meta)

        state_extra = {"outlinks": page_links[:400], "last_status": fr.status_code,
                       "sitemap_lastmod": self.sitemap_lastmod.get(url, ""),
                       "children": pdf_links,
                       # parent metadata for its PDFs when the page itself is later a 304
                       "meta": {k: parent_meta.get(k) for k in ("title", "authors", "author_urls", "date",
                                                                "categories", "document_series", "description")}}
        etag, last_mod = fr.headers.get("ETag", ""), fr.headers.get("Last-Modified", "")

        if self._is_oped_listing(url) and not self.audit_only:
            oped_docs, entry_urls = self.process_oped_listing(url, html)
            docs.extend(oped_docs)
            state_extra["children"] = sorted(set(pdf_links) | set(entry_urls))

        if doc is None:
            self._bump({"too_short": "skipped_short", "audit": "unchanged"}.get(reason, "skipped_nav"))
            rec["outcome"] = reason
            self.state.record(url, content_hash=rec["text_hash"], document_id="", etag=etag,
                              last_modified=last_mod, changed=False, **state_extra)
            if reason == "too_short":
                self._change("rejected", url, reason=f"extracted text shorter than {self.site.min_text_len} chars")
            return docs, page_links

        prev_hash = self.state.stored_hash(url)
        if self.incremental and prev_hash == doc["content_hash"]:
            self._bump("unchanged")
            rec["outcome"] = "unchanged"
            self.state.record(url, content_hash=doc["content_hash"],
                              document_id=self.state.document_id(url) or doc["document_id"],
                              etag=etag, last_modified=last_mod, changed=False, **state_extra)
            return docs, page_links

        changed = bool(prev_hash) and bool(self.state.document_id(url))
        self._bump("updated" if changed else "added")
        rec["outcome"] = "modified" if changed else "new"
        self._retire_if_replaced(url, doc["document_id"])
        self.state.record(url, content_hash=doc["content_hash"], document_id=doc["document_id"],
                          etag=etag, last_modified=last_mod, changed=True, **state_extra)
        self._change("modified" if changed else "new", url, doc)
        docs.append(doc)
        return docs, page_links

    # ── main loop ────────────────────────────────────────────────────────────────
    def _enqueue(self, url: str, depth: int, method: str, referrer: str = "") -> None:
        self._note_discovery(url, method, referrer)
        with self._lock:
            if url in self.enqueued:
                return
            self.enqueued.add(url)
            self.depth[url] = depth
            self._rec(url)["depth"] = depth
            self.frontier.append(url)

    def _check_entry_point(self) -> None:
        """Abort early (changing nothing) if the base URL is unreachable / unauthorised."""
        fr = self.fetch(self.site.base_url, conditional=False)
        if fr.status_code in (401, 403):
            raise CrawlAborted(f"{self.site.source}: authentication failed (HTTP {fr.status_code}) "
                               f"at {self.site.base_url}")
        if fr.status_code is None or fr.status_code >= 500:
            raise CrawlAborted(f"{self.site.source}: entry point unreachable "
                               f"({fr.error or fr.status_code})")

    def _migrate_state_keys(self) -> None:
        """Re-key legacy (non-canonical) state URLs so identities stay stable."""
        for u in list(self.state.urls):
            cu = canonicalize_url(u)
            if cu and cu != u:
                rec = self.state.urls.pop(u)
                self.state.urls.setdefault(cu, rec)
        for rec in self.state.urls.values():
            for k in ("outlinks", "children"):
                if rec.get(k):
                    rec[k] = sorted({canonicalize_url(x) or x for x in rec[k]})

    def crawl(self) -> CrawlResult:
        self._migrate_state_keys()
        self.state.begin_run()
        self._check_entry_point()
        self._load_robots()

        self._enqueue(self.base_canonical, 0, "seed")
        for u, _ in self.site.listing_urls:
            cu = canonicalize_url(u)
            if cu:
                self._enqueue(cu, 0, "listing_seed")
        for u in self.discover_sitemap():
            if self._should_follow(u):
                self._enqueue(u, 0, "sitemap")
            elif is_pdf_url(u) and self.site.include_pdfs:
                self._note_discovery(u, "sitemap")
                self._queue_pdf(u, {"url": ""})
        for u in self.site.extra_seeds:
            cu = canonicalize_url(u)
            if not cu or not same_site(cu, self.site.domain):
                continue
            if is_pdf_url(cu):
                if self.site.include_pdfs:
                    self._note_discovery(cu, "extra_seed")
                    self._queue_pdf(cu, {"url": ""})
            elif self._should_follow(cu):
                self._enqueue(cu, 0, "extra_seed")
        for u, rec in list(self.state.urls.items()):
            if not same_site(u, self.site.domain):
                continue
            cu = canonicalize_url(u)
            if not cu:
                continue
            if is_pdf_url(cu):
                # Known PDFs are re-checked every run even if no page links them any more
                # (an unlinked-but-live document must not be retired).
                if self.site.include_pdfs and rec.get("document_id"):
                    parent = rec.get("parent", "")
                    meta = (self.state.get(parent).get("meta") or {}) if parent else {}
                    self._note_discovery(cu, "known_state")
                    self._queue_pdf(cu, {"url": parent, **meta})
            elif self._should_follow(cu):
                self._enqueue(cu, 0, "known_state")

        self.result.discovered = len(self.frontier)
        self._say(f"[{self.site.source}] seeded {len(self.frontier)} URLs; crawling…")

        processed = 0
        workers = max(1, int(self.site.max_workers or 4))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while self.frontier and processed < self.site.max_pages:
                batch = []
                while self.frontier and len(batch) < workers * 2 and processed + len(batch) < self.site.max_pages:
                    u = self.frontier.popleft()
                    with self._lock:
                        if u in self.processed_urls:
                            continue
                        self.processed_urls.add(u)
                    batch.append(u)
                futures = {ex.submit(self.process, u): u for u in batch}
                for fut in as_completed(futures):
                    u = futures[fut]
                    processed += 1
                    try:
                        docs, links = fut.result()
                    except Exception as exc:          # a bug on one page never kills the run
                        logger.exception(f"[{self.site.source}] error on {u}: {exc}")
                        self._rec(u).update(outcome="error", error=str(exc)[:300])
                        self.state.mark_unavailable(u, "exception")
                        self._bump("failed")
                        continue
                    self.result.docs.extend(docs)
                    d = self.depth.get(u, 0)
                    if d < self.site.max_depth:
                        for link in links:
                            if self._should_follow(link):
                                self._enqueue(link, d + 1, "links", u)
                            else:
                                self._note_discovery(link, "links", u)
                    else:
                        for link in links:
                            self._note_discovery(link, "links", u)
                    if processed % 50 == 0:
                        self._say(f"[{self.site.source}] processed {processed} pages "
                                  f"(+{self._counts['added']} new, ~{self._counts['updated']} changed, "
                                  f"={self._counts['unchanged']} unchanged, !{self._counts['failed']} failed)")
                self.state.save()

        # PDFs (discovered from any page or the sitemap).
        if self.site.include_pdfs and self._pdf_queue:
            self._say(f"[{self.site.source}] checking {len(self._pdf_queue)} PDFs…")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.process_pdf, u, p): u for u, p in self._pdf_queue.items()}
                for fut in as_completed(futs):
                    try:
                        d = fut.result()
                    except Exception as exc:
                        logger.exception(f"[{self.site.source}] PDF error {futs[fut]}: {exc}")
                        self._bump("failed")
                        continue
                    if d:
                        self.result.docs.append(d)
            self.state.save()

        self.result.complete = processed < self.site.max_pages and not self.frontier
        self._handle_removals(processed)
        if self._replaced_ids and not self.audit_only:   # safe regardless of completeness
            self.result.removed_ids = sorted(set(self.result.removed_ids) | self._replaced_ids)
            self._counts["replaced_ids"] = len(self._replaced_ids)
        self.state.save()
        self.result.counts = dict(self._counts, processed=processed, pdfs_checked=len(self._pdf_queue))
        self.result.log = self.log
        self.result.status = "success" if self.result.complete else "partial"
        logger.info(f"[{self.site.source}] crawl done: {self.result.counts}")
        c = self._counts
        self._say(f"[{self.site.source}] ✓ crawl done — {c['added']} new, {c['updated']} changed, "
                  f"{c['unchanged']} unchanged, {c['removed']} removed, {c['failed']} failed, "
                  f"{c['pdf_added']}+{c['pdf_updated']} PDFs, {c['oped_added']}+{c['oped_updated']} op-eds")
        return self.result

    def _handle_removals(self, processed: int) -> None:
        """Retire documents only after repeated, trustworthy evidence of removal."""
        if self.audit_only:
            return
        attempted = max(1, processed + len(self._pdf_queue))
        failure_ratio = self._counts["failed"] / attempted
        if not self.result.complete:
            logger.warning(f"[{self.site.source}] crawl incomplete — removal detection skipped")
            return
        if failure_ratio > config.KB_MAX_FAILURE_RATIO:
            logger.warning(f"[{self.site.source}] failure ratio {failure_ratio:.0%} > "
                           f"{config.KB_MAX_FAILURE_RATIO:.0%} — removal detection skipped")
            return
        unseen = self.state.unseen_urls()
        confirmed = self.state.confirm_missing(unseen, config.KB_REMOVAL_CONFIRMATIONS)
        pending = [u for u in unseen if u not in confirmed]
        self._counts["removal_pending"] = len(pending)
        for u in pending:
            self._change("removal_pending", u,
                         missing_count=self.state.get(u).get("missing_count", 0))
        removed = []
        for u in list(confirmed):
            # Final check before retiring anything: fetch the URL itself. Only a real
            # 404/410 means "permanently removed"; a live-but-unlinked URL is kept.
            if not same_site(u, self.site.domain):
                continue            # external op-ed links: absence from the listing is the signal
            fr = self.fetch(u, conditional=False)
            if fr.status_code not in (404, 410):
                confirmed.remove(u)
                self.state.mark_seen(u, with_children=False)
                self._change("unlinked_but_live" if fr.status_code == 200 else "removal_deferred", u,
                             status=fr.status_code)
                continue
        for u in confirmed:
            did = self.state.document_id(u)
            if did:
                removed.append(did)
                self._change("removed", u, document_id=did,
                             redirected_to=self.state.get(u).get("redirected_to", ""))
            self.state.remove(u)
        # Redirected URLs whose target is live: retire the old document now.
        for u, rec in list(self.state.urls.items()):
            tgt = rec.get("redirected_to")
            if tgt and self.state.seen_this_run(tgt) and rec.get("document_id") \
                    and rec["document_id"] != self.state.document_id(tgt):
                removed.append(rec["document_id"])
                self.state.remove(u)
        self.result.removed_ids = sorted(set(removed))
        self._counts["removed"] = len(self.result.removed_ids)


def _infer_source_type(url: str, source: str) -> str:
    """Back-compat wrapper around the schema classifier."""
    return classify_content_type(url, source)


_EXTRACTOR_MODULES = ("site_extract.py", "metadata.py", "url_utils.py")


def extractor_version() -> str:
    """
    Fingerprint of the code that turns fetched HTML into documents. Incremental runs
    skip unchanged pages (304 / same content hash), so an extraction fix would never
    reach them; when this fingerprint differs from the one stored in the crawl state,
    the next crawl re-extracts every page once. Unchanged chunks are still served
    from the embedding cache, so only genuinely different chunks are re-embedded.
    """
    h = hashlib.sha256()
    src_dir = Path(__file__).resolve().parent.parent / "src"
    for name in _EXTRACTOR_MODULES:
        h.update(name.encode())
        h.update((src_dir / name).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def crawl_site(site: SiteConfig, incremental: bool = True, progress_cb: Optional[Callable] = None,
               audit_only: bool = False) -> CrawlResult:
    """Build state + engine and run one crawl. CrawlAborted → an 'aborted' result."""
    if audit_only:
        # Audits never touch the production crawl state.
        state = CrawlState(site.source, path=Path(tempfile.mkdtemp()) / "audit_state.json")
        state.urls = {}
        incremental = False
    else:
        state = CrawlState(site.source)
    current_extractor = extractor_version()
    if incremental and state.urls and state.extractor_version != current_extractor:
        msg = (f"[{site.source}] extraction code changed since the last crawl — "
               "re-extracting every page once (full crawl).")
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)
        incremental = False
    engine = CrawlEngine(site, state, incremental=incremental, progress_cb=progress_cb,
                         audit_only=audit_only)
    try:
        result = engine.crawl()
        if not audit_only and result.status == "success":
            state.extractor_version = current_extractor
            state.save()
        return result
    except CrawlAborted as exc:
        logger.error(str(exc))
        if progress_cb:
            progress_cb(f"⚠️  {exc}")
        return CrawlResult(status="aborted", error=str(exc), complete=False, log=engine.log,
                           counts={"failed": 1})
