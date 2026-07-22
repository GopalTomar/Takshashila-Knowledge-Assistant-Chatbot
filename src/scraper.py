"""
scraper.py — Polite, multi-strategy web scraper for the public Takshashila
Institution website (https://takshashila.org.in/).

Discovery methods (Part 2 of the KB robustness upgrade):
    1. Sitemap discovery   — sitemap.xml / sitemap_index.xml / wp-sitemap.xml,
                              recursively expanded, with source_type inferred
                              from the URL path.
    2. RSS discovery       — existing feeds, kept as-is.
    3. Listing-page discovery — existing paginated listings (publications,
                              blogs, ...), kept as-is.
    4. Internal crawl fallback — a capped, same-domain BFS crawl that only
                              keeps pages matching known content-path patterns
                              and drops navigation/chrome/tag/author/search
                              pages.
    5. PDF detection + download, with the linking article URL and (when
       available) page numbers preserved by src/extractors.py.

All discovered stubs converge on the same per-article scraper
(``_scrape_article``), so metadata extraction, mojibake repair, and dedup
logic only exist in one place. A manifest describing what was scraped,
skipped, failed, and updated is written to config.WEBSITE_MANIFEST_FILE on
every run.
"""

import csv
import json
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from src import config
from src.utils import (
    append_jsonl, clean_document_metadata, clean_text, content_hash, get_logger,
    is_same_domain, load_jsonl, looks_like_pdf, normalize_url,
    now_iso, parse_date, save_jsonl, truncate, url_hash, with_retry,
)

logger = get_logger("scraper", config.SCRAPE_LOG)


# ── HTTP session ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return s


SESSION = _make_session()


def _get(url: str) -> requests.Response:
    """GET with retries and timeout. Does NOT retry on 404."""
    def _req():
        r = SESSION.get(url, timeout=config.SCRAPE_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        # Force correct text decoding: prefer the charset sniffed from the body
        # over requests' ISO-8859-1 default, which is the root cause of mojibake.
        ctype = r.headers.get("Content-Type", "").lower()
        if "application/pdf" not in ctype and "octet-stream" not in ctype:
            r.encoding = r.apparent_encoding or r.encoding or "utf-8"
        return r

    last_exc = None
    for attempt in range(1, config.SCRAPE_MAX_RETRIES + 1):
        try:
            return _req()
        except requests.exceptions.HTTPError as exc:
            # Never retry client errors (4xx) — they won't change
            raise exc
        except Exception as exc:
            last_exc = exc
            wait = 2.0 ** attempt
            logger.warning(f"Attempt {attempt} failed: {exc}. Retrying in {wait:.1f}s…")
            time.sleep(wait)
    raise last_exc


def _polite_get(url: str) -> Optional[requests.Response]:
    """GET with rate limiting; returns None on failure."""
    time.sleep(config.SCRAPE_DELAY)
    try:
        return _get(url)
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            # 404 is definitive — no point retrying
            return None
        logger.warning(f"Failed to fetch {url}: {exc}")
        return None
    except Exception as exc:
        logger.warning(f"Failed to fetch {url}: {exc}")
        return None


# ── Failed-URL logging ─────────────────────────────────────────────────────────

def _log_failed(url: str, reason: str):
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not config.FAILED_CSV.exists()
    with open(config.FAILED_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["url", "reason", "timestamp"])
        w.writerow([url, reason, now_iso()])


# ── URL classification helpers ──────────────────────────────────────────────────

def _path_of(url: str) -> str:
    try:
        return (urlparse(url).path or "/").lower()
    except Exception:
        return ""


def infer_source_type_from_url(url: str) -> str:
    """Best-effort content type from the URL path (used by sitemap + crawl)."""
    path = _path_of(url)
    mapping = [
        ("/pages/publications/", "publication"), ("/publications/", "publication"),
        ("/pages/blogs/", "blog"), ("/blogs/", "blog"),
        ("/research/", "research"), ("/commentary/", "commentary"),
        ("/policy/", "policy"), ("/papers/", "paper"),
        ("/reports/", "report"), ("/briefs/", "brief"),
        ("/articles/", "article"), ("/content/", "article"),
    ]
    for needle, stype in mapping:
        if needle in path:
            return stype
    return "page"


def is_excluded_url(url: str) -> bool:
    """True if this URL is chrome/navigation/admin/search and should never be
    kept as a final content document (Part 2, discovery method 4 exclusions)."""
    path = _path_of(url)
    full = (url or "").lower()
    for pattern in config.WEBSITE_EXCLUDE_PATH_PATTERNS:
        if pattern.startswith("?"):
            if pattern in full:
                return True
        elif pattern in path or pattern in full:
            return True
    if any(path.endswith(ext) for ext in config.WEBSITE_SKIP_EXTENSIONS):
        return True
    return False


def is_useful_content_url(url: str) -> bool:
    """True if the URL path matches a known useful-content pattern
    (Part 2, discovery method 4: crawl fallback keep-list)."""
    path = _path_of(url)
    if is_excluded_url(url):
        return False
    return any(p in path for p in config.WEBSITE_INCLUDE_PATH_PATTERNS)


# ── Sitemap discovery ────────────────────────────────────────────────────────────

def _fetch_xml(url: str) -> Optional[BeautifulSoup]:
    resp = _polite_get(url)
    if resp is None:
        return None
    try:
        return BeautifulSoup(resp.content, "xml")
    except Exception:
        try:
            return BeautifulSoup(resp.content, "lxml-xml")
        except Exception as exc:
            logger.warning(f"Could not parse XML at {url}: {exc}")
            return None


def _collect_from_sitemaps(progress_cb: Optional[Callable] = None,
                            max_urls: int = 5000) -> List[Dict]:
    """
    Discover article/page URLs from sitemap.xml / sitemap_index.xml / WordPress
    wp-sitemap.xml. Recursively expands nested <sitemapindex> entries. Silently
    skips any sitemap URL that 404s or fails to parse (Part 2, method 1).
    """
    stubs: List[Dict] = []
    seen_urls: Set[str] = set()
    seen_sitemaps: Set[str] = set()
    queue: deque = deque(config.WEBSITE_SITEMAP_URLS)
    tried_any = False

    while queue and len(stubs) < max_urls:
        sm_url = queue.popleft()
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        soup = _fetch_xml(sm_url)
        if soup is None:
            continue
        tried_any = True

        # Nested sitemap index → enqueue children.
        nested = soup.find_all("sitemap")
        if nested:
            for node in nested:
                loc = node.find("loc")
                if loc and loc.text:
                    queue.append(loc.text.strip())
            if progress_cb:
                progress_cb(f"Sitemap index: {sm_url} → {len(nested)} nested sitemaps")
            continue

        # Leaf sitemap → collect <url><loc>.
        url_nodes = soup.find_all("url")
        count = 0
        for node in url_nodes:
            loc = node.find("loc")
            if not loc or not loc.text:
                continue
            href = normalize_url(loc.text.strip(), config.WEBSITE_BASE_URL)
            if href in seen_urls or not is_same_domain(href) or looks_like_pdf(href):
                continue
            if is_excluded_url(href):
                continue
            seen_urls.add(href)
            lastmod_el = node.find("lastmod")
            stubs.append({
                "url": href,
                "title": "",
                "date": parse_date(lastmod_el.text) if lastmod_el else "",
                "category": "",
                "source_type": infer_source_type_from_url(href),
                "discovery_method": "sitemap",
            })
            count += 1
            if len(stubs) >= max_urls:
                break
        if progress_cb and count:
            progress_cb(f"Sitemap: {sm_url} → {count} URLs")

    if progress_cb:
        if tried_any:
            progress_cb(f"Sitemap discovery total: {len(stubs)} URLs")
        else:
            progress_cb("Sitemap discovery: no reachable sitemap found (skipped)")
    return stubs

def _extract_article_links_from_html(html: str, base_url: str) -> List[Dict]:
    """
    Parse a listing page and return list of {url, title, date, category, source_type}.
    Handles WordPress-style post listings common on takshashila.org.in.
    """
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen = set()

    # Try common WordPress selectors
    selectors = [
        "article",
        "div.post",
        "div.entry",
        "div.blog-post",
        "li.post",
        "div.td-block-span12",
        "div.jeg_post",
    ]

    items = []
    for sel in selectors:
        items = soup.select(sel)
        if items:
            break

    # Fallback: grab all h2/h3 links in main content area
    if not items:
        main = soup.find("main") or soup.find("div", id="content") or soup
        items = main.find_all(["h2", "h3"])

    for item in items:
        a_tag = item.find("a", href=True) if item.name not in ["h2", "h3"] else item.find("a", href=True)
        if not a_tag:
            continue
        href = normalize_url(a_tag["href"], base_url)
        if not is_same_domain(href):
            continue
        if looks_like_pdf(href):
            continue
        if href in seen:
            continue
        seen.add(href)

        title = clean_text(a_tag.get_text())
        date_el = item.find(class_=lambda c: c and any(x in c for x in ["date", "time", "published"]))
        date = parse_date(date_el.get_text() if date_el else "")
        cat_el = item.find(class_=lambda c: c and "categ" in str(c))
        category = clean_text(cat_el.get_text()) if cat_el else ""

        source_type = "publication" if "publication" in base_url else "blog"

        articles.append({
            "url": href,
            "title": title or href,
            "date": date,
            "category": category,
            "source_type": source_type,
        })

    return articles


def _collect_from_rss() -> List[Dict]:
    """Parse RSS feeds and return article stubs."""
    articles = []
    for feed_url in config.RSS_FEEDS:
        try:
            logger.info(f"Parsing RSS: {feed_url}")
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                url = getattr(entry, "link", "")
                if not url or not is_same_domain(url):
                    continue
                title = getattr(entry, "title", url)
                date = parse_date(
                    getattr(entry, "published", "") or getattr(entry, "updated", "")
                )
                tags = [t.term for t in getattr(entry, "tags", [])]
                category = ", ".join(tags)
                # Guess source type from URL
                source_type = "blog"
                if "publication" in url or "research" in url or "brief" in url:
                    source_type = "publication"

                articles.append({
                    "url": url,
                    "title": clean_text(title),
                    "date": date,
                    "category": category,
                    "source_type": source_type,
                    "tags": tags,
                })
        except Exception as exc:
            logger.warning(f"RSS parse error for {feed_url}: {exc}")
    return articles


def _paginate_listing(base_url: str, source_type: str,
                      progress_cb: Optional[Callable] = None) -> List[Dict]:
    """
    Fetch all paginated listing pages and collect article stubs.
    Handles ?page=N and /page/N/ WordPress patterns.
    """
    articles = []
    seen_urls: Set[str] = set()
    page = 1
    consecutive_empty = 0

    while True:
        # Try both pagination patterns
        if page == 1:
            url = base_url
        else:
            # WordPress /page/N/
            url = base_url.rstrip("/") + f"/page/{page}/"

        logger.info(f"Fetching listing page {page}: {url}")
        resp = _polite_get(url)
        if resp is None or resp.status_code == 404:
            break

        items = _extract_article_links_from_html(resp.text, url)
        new_items = [i for i in items if i["url"] not in seen_urls]

        if not new_items:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
        else:
            consecutive_empty = 0

        for item in new_items:
            seen_urls.add(item["url"])
            item["source_type"] = source_type
            articles.append(item)

        if progress_cb:
            progress_cb(f"Listing page {page}: found {len(new_items)} new articles")

        # Stop if fewer items than expected on a page (last page)
        if len(items) < 3:
            break
        page += 1
        if page > 100:   # safety cap
            break

    logger.info(f"Collected {len(articles)} articles from {base_url}")
    return articles


# ── Article page scraper ───────────────────────────────────────────────────────

def _extract_main_text(soup: BeautifulSoup) -> str:
    """Shared main-content extraction, used by both the article scraper and
    the internal crawl fallback so text-quality logic lives in one place."""
    for sel in ["article", "div.entry-content", "div.post-content",
                "div.td-post-content", "div.content-inner", "main"]:
        el = soup.select_one(sel)
        if el:
            text = clean_text(el.get_text(separator="\n"))
            if text:
                return text
    return clean_text(soup.get_text(separator="\n"))


def _fetch_and_cache_html(url: str) -> Optional[str]:
    """Fetch a URL's HTML, using the on-disk cache keyed by url_hash if present.
    Shared by the article scraper and the crawl fallback so a page fetched
    during discovery is never re-downloaded during full extraction."""
    uid = url_hash(url)
    html_path = config.RAW_HTML_DIR / f"{uid}.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8", errors="replace")
    resp = _polite_get(url)
    if resp is None:
        _log_failed(url, "HTTP error")
        return None
    html_path.write_text(resp.text, encoding="utf-8", errors="replace")
    return resp.text


def _crawl_fallback(
    seed_urls: List[str],
    already_seen: Set[str],
    max_pages: int,
    max_depth: int,
    progress_cb: Optional[Callable] = None,
) -> List[Dict]:
    """
    Controlled same-domain BFS crawl (Part 2, discovery method 4). Starts from
    ``seed_urls`` (base URL + known listing/sitemap hubs) and follows same-
    domain links up to ``max_depth``, keeping only pages whose URL matches a
    known useful-content pattern and that have enough extracted text. Safety
    caps: max_pages total fetches, max_depth link-hops, hard iteration limit.
    """
    visited: Set[str] = set(already_seen)
    frontier: deque = deque((u, 0) for u in seed_urls if u not in visited)
    for u in seed_urls:
        visited.add(u)

    kept: List[Dict] = []
    fetched = 0
    hard_cap = max(max_pages * 4, 200)   # frontier can explore hubs beyond kept pages

    while frontier and fetched < hard_cap and len(kept) < max_pages:
        url, depth = frontier.popleft()

        html = _fetch_and_cache_html(url)
        fetched += 1
        if html is None:
            continue

        soup = BeautifulSoup(html, "lxml")

        if is_useful_content_url(url):
            text = _extract_main_text(soup)
            if len(text) >= config.WEBSITE_MIN_TEXT_LEN:
                og = soup.find("meta", property="og:title")
                title = (og.get("content", "") if og else "") or ""
                if not title:
                    h1 = soup.find("h1")
                    title = clean_text(h1.get_text()) if h1 else url
                kept.append({
                    "url": url,
                    "title": clean_text(title),
                    "date": "",
                    "category": "",
                    "source_type": infer_source_type_from_url(url),
                    "discovery_method": "crawl",
                })
                if progress_cb and len(kept) % 10 == 0:
                    progress_cb(f"Crawl fallback: kept {len(kept)} content pages "
                                f"(fetched {fetched})")

        if depth >= max_depth:
            continue

        for a in soup.find_all("a", href=True):
            href = normalize_url(a["href"], url).split("#")[0]
            if not href or href in visited:
                continue
            if not is_same_domain(href):
                continue
            if looks_like_pdf(href):
                continue
            if is_excluded_url(href):
                continue
            visited.add(href)
            frontier.append((href, depth + 1))

    if progress_cb:
        progress_cb(f"Crawl fallback complete: {len(kept)} useful pages "
                    f"from {fetched} pages fetched")
    return kept


def _scrape_article(stub: Dict, existing_hashes: Set[str],
                    progress_cb: Optional[Callable] = None) -> Optional[Dict]:
    """
    Fetch a single article page, extract content & metadata, detect PDFs.
    Returns a document dict, or None if skipped/failed.
    """
    url = stub["url"]
    uid = url_hash(url)
    html_path = config.RAW_HTML_DIR / f"{uid}.html"

    html = _fetch_and_cache_html(url)
    if html is None:
        return None

    soup = BeautifulSoup(html, "lxml")

    # ── Extract main text ──────────────────────────────────────────────────
    text = _extract_main_text(soup)

    if len(text) < 100:
        logger.warning(f"Very short text for {url} ({len(text)} chars); keeping anyway")

    chash = content_hash(text)
    if chash in existing_hashes:
        logger.info(f"Duplicate content, skipping: {url}")
        return None

    # ── Extract metadata ───────────────────────────────────────────────────
    title = stub.get("title", "")
    if not title:
        og = soup.find("meta", property="og:title")
        if og:
            title = og.get("content", "")
        if not title:
            h1 = soup.find("h1")
            title = clean_text(h1.get_text()) if h1 else url

    author = ""
    for a_sel in [
        "span.author", "a[rel='author']", ".post-author",
        ".jeg_post_meta_author", "[class*='author']"
    ]:
        el = soup.select_one(a_sel)
        if el:
            author = clean_text(el.get_text())
            break
    if not author:
        meta = soup.find("meta", {"name": "author"})
        if meta:
            author = meta.get("content", "")

    date = stub.get("date", "")
    if not date:
        for d_sel in ["time", ".entry-date", ".post-date", ".published"]:
            el = soup.select_one(d_sel)
            if el:
                date = parse_date(el.get("datetime", "") or el.get_text())
                break

    category = stub.get("category", "")
    tags = stub.get("tags", [])
    if not tags:
        tag_els = soup.select("a[rel='tag'], .tags a, .post-tags a")
        tags = [clean_text(t.get_text()) for t in tag_els]

    # ── Detect PDF links ───────────────────────────────────────────────────
    pdf_urls = []
    for a in soup.find_all("a", href=True):
        href = normalize_url(a["href"], url)
        if looks_like_pdf(href) and is_same_domain(href):
            pdf_urls.append(href)

    canonical_url = url
    canon_el = soup.find("link", rel="canonical")
    if canon_el and canon_el.get("href"):
        canonical_url = normalize_url(canon_el["href"], url)

    source_type = stub.get("source_type") or infer_source_type_from_url(url) or "page"

    doc = {
        "document_id":       f"website_{uid}",
        "url_hash":          uid,
        "original_url":      url,
        "url":               url,
        "canonical_url":     canonical_url,
        "title":             title,
        "author":            author,
        "date":              date,
        "category":          category or source_type,
        "tags":              tags,
        "source":            "website",
        "source_name":       "Takshashila Website",
        "source_type":       source_type,
        "text":              text,
        "text_length":       len(text),
        "content_hash":      chash,
        "pdf_urls":          pdf_urls,
        "pdf_url":           pdf_urls[0] if pdf_urls else "",
        "local_html_path":   str(config.RAW_HTML_DIR / f"{uid}.html"),
        "discovery_method":  stub.get("discovery_method", "listing"),
        "extraction_method": "bs4-selector",
        "scraped_at":        now_iso(),
        "updated_at":        now_iso(),
    }

    if progress_cb:
        progress_cb(f"Scraped: {title[:60]}")

    return doc


# ── PDF downloader ─────────────────────────────────────────────────────────────

def _download_pdf(pdf_url: str) -> Optional[Path]:
    """Download a PDF and return its local path, or None on failure."""
    uid = url_hash(pdf_url)
    pdf_path = config.RAW_PDF_DIR / f"{uid}.pdf"
    if pdf_path.exists():
        logger.debug(f"PDF already downloaded: {pdf_url}")
        return pdf_path

    logger.info(f"Downloading PDF: {pdf_url}")
    resp = _polite_get(pdf_url)
    if resp is None:
        _log_failed(pdf_url, "PDF download failed")
        return None

    if "pdf" not in resp.headers.get("Content-Type", "").lower() and \
       not pdf_url.lower().endswith(".pdf"):
        logger.warning(f"Unexpected Content-Type for PDF: {pdf_url}")

    pdf_path.write_bytes(resp.content)
    logger.info(f"Saved PDF: {pdf_path.name} ({len(resp.content)//1024} KB)")
    return pdf_path


# ── Manifest ─────────────────────────────────────────────────────────────────────

def _write_manifest(manifest: Dict):
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        config.WEBSITE_MANIFEST_FILE.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning(f"Could not write website manifest: {exc}")


# ── Main scrape function ───────────────────────────────────────────────────────

def run_scrape(
    incremental: bool = True,
    progress_cb: Optional[Callable] = None,
    full: Optional[bool] = None,
    max_pages: Optional[int] = None,
    max_depth: Optional[int] = None,
    include_pdfs: Optional[bool] = None,
    use_crawl_fallback: bool = True,
    use_sitemap: bool = True,
    dry_run: bool = False,
) -> Dict:
    """
    Full multi-strategy scrape pipeline for the public Takshashila website.

    Discovery order: RSS → known listing pages → sitemap.xml → internal
    same-domain crawl fallback (only kept for URLs matching known content
    patterns). All stubs are deduplicated by URL before per-article scraping.

    Args:
        incremental:  skip URLs already present in documents.jsonl (by url_hash).
        full:         alias for incremental=False (kept for the --full CLI flag).
        max_pages:    cap on pages kept by the crawl fallback (default: config).
        max_depth:    cap on link-hops for the crawl fallback (default: config).
        include_pdfs: whether to download+queue linked PDFs (default: config).
        use_crawl_fallback / use_sitemap: allow disabling either discovery
            method (e.g. for a fast dry-run).
        dry_run:      discover + report counts only; write nothing to disk.

    Returns a summary dict with counts, discovery breakdown, and manifest path.
    """
    if full is not None:
        incremental = not full
    max_pages = config.WEBSITE_MAX_PAGES if max_pages is None else max_pages
    max_depth = config.WEBSITE_MAX_DEPTH if max_depth is None else max_depth
    include_pdfs = config.WEBSITE_INCLUDE_PDFS if include_pdfs is None else include_pdfs

    logger.info("═" * 60)
    logger.info("Starting Takshashila website scrape")
    logger.info(f"incremental={incremental} max_pages={max_pages} "
                f"max_depth={max_depth} include_pdfs={include_pdfs} dry_run={dry_run}")

    started_at = now_iso()

    # Load existing documents to detect duplicates. Some records in
    # documents.jsonl (e.g. the unified Commit KB / website docs written by
    # build_knowledge_base.py) don't carry url_hash/content_hash, so skip those
    # instead of KeyError-ing.
    existing_docs = load_jsonl(config.DOCUMENTS_FILE) if incremental else []
    existing_url_hashes: Set[str] = {
        d["url_hash"] for d in existing_docs if d.get("url_hash")
    }
    existing_content_hashes: Set[str] = {
        d["content_hash"] for d in existing_docs if d.get("content_hash")
    }
    existing_urls: Set[str] = {
        d.get("original_url") or d.get("url") for d in existing_docs
        if d.get("original_url") or d.get("url")
    }

    # ── 1+2+3) Collect stubs: RSS, listing pages, sitemap ────────────────────
    if progress_cb:
        progress_cb("Discovering content URLs (RSS, listings, sitemap)…")

    stubs: List[Dict] = []
    discovery_counts: Dict[str, int] = {}

    rss_stubs = _collect_from_rss()
    for s in rss_stubs:
        s.setdefault("discovery_method", "rss")
    stubs.extend(rss_stubs)
    discovery_counts["rss"] = len(rss_stubs)

    listing_total = 0
    for url, stype in config.WEBSITE_LISTING_URLS:
        listing_stubs = _paginate_listing(url, stype, progress_cb)
        for s in listing_stubs:
            s.setdefault("discovery_method", "listing")
        stubs.extend(listing_stubs)
        listing_total += len(listing_stubs)
    discovery_counts["listing"] = listing_total

    sitemap_stubs = []
    if use_sitemap:
        sitemap_stubs = _collect_from_sitemaps(progress_cb)
        stubs.extend(sitemap_stubs)
    discovery_counts["sitemap"] = len(sitemap_stubs)

    # Deduplicate stubs by URL (first discovery method wins the metadata).
    seen: Set[str] = set()
    unique_stubs: List[Dict] = []
    for s in stubs:
        if s["url"] not in seen:
            seen.add(s["url"])
            unique_stubs.append(s)

    # ── 4) Internal same-domain crawl fallback ───────────────────────────────
    crawl_stubs = []
    if use_crawl_fallback:
        if progress_cb:
            progress_cb(f"Running internal crawl fallback (max_pages={max_pages}, "
                        f"max_depth={max_depth})…")
        seed_urls = [config.WEBSITE_BASE_URL] + [u for u, _ in config.WEBSITE_LISTING_URLS]
        crawl_stubs = _crawl_fallback(
            seed_urls=seed_urls,
            already_seen=seen,
            max_pages=max_pages,
            max_depth=max_depth,
            progress_cb=progress_cb,
        )
        for s in crawl_stubs:
            if s["url"] not in seen:
                seen.add(s["url"])
                unique_stubs.append(s)
    discovery_counts["crawl_fallback"] = len(crawl_stubs)

    logger.info(f"Total unique content URLs discovered: {len(unique_stubs)} "
                f"({discovery_counts})")
    if progress_cb:
        progress_cb(f"Found {len(unique_stubs)} unique content URLs "
                    f"(rss={discovery_counts['rss']}, listing={discovery_counts['listing']}, "
                    f"sitemap={discovery_counts['sitemap']}, "
                    f"crawl={discovery_counts['crawl_fallback']})")

    manifest = {
        "started_at": started_at,
        "mode": "full" if not incremental else "incremental",
        "dry_run": dry_run,
        "discovery_counts": discovery_counts,
        "total_discovered": len(unique_stubs),
        "scraped": [], "skipped": [], "failed": [], "updated": [],
    }

    if dry_run:
        manifest["completed_at"] = now_iso()
        manifest["note"] = "dry_run — no pages fetched, no files written"
        _write_manifest(manifest)
        summary = {
            "new_articles": 0, "skipped": 0, "failed": 0, "pdf_downloaded": 0,
            "total_documents": len(existing_docs),
            "discovered": len(unique_stubs), "discovery_counts": discovery_counts,
            "dry_run": True, "manifest_path": str(config.WEBSITE_MANIFEST_FILE),
        }
        if progress_cb:
            progress_cb(f"✓ Dry run complete — {len(unique_stubs)} URLs would be processed")
        return summary

    # ── Scrape each article ────────────────────────────────────────────────
    new_docs = []
    skipped = 0
    failed = 0

    for i, stub in enumerate(unique_stubs):
        uid = url_hash(stub["url"])
        if incremental and uid in existing_url_hashes:
            skipped += 1
            manifest["skipped"].append(stub["url"])
            continue

        if progress_cb and i % 10 == 0:
            progress_cb(f"Scraping content page {i+1}/{len(unique_stubs)}…")

        doc = _scrape_article(stub, existing_content_hashes, progress_cb)
        if doc is None:
            failed += 1
            manifest["failed"].append(stub["url"])
            continue

        doc = clean_document_metadata(doc)   # repair any mojibake before storing
        new_docs.append(doc)
        existing_url_hashes.add(uid)
        existing_content_hashes.add(doc["content_hash"])
        append_jsonl(config.DOCUMENTS_FILE, doc)
        if stub["url"] in existing_urls:
            manifest["updated"].append(stub["url"])
        else:
            manifest["scraped"].append(stub["url"])

    # ── Download PDFs ──────────────────────────────────────────────────────
    pdf_downloaded = 0
    pdf_docs = []

    all_docs = existing_docs + new_docs
    if include_pdfs:
        for doc in all_docs:
            for pdf_url in doc.get("pdf_urls", []):
                pdf_path = _download_pdf(pdf_url)
                if pdf_path:
                    pdf_downloaded += 1
                    # Create a separate doc entry for the PDF
                    pdf_uid = url_hash(pdf_url)
                    if pdf_uid not in existing_url_hashes:
                        pdf_doc = {
                            "document_id": f"website_pdf_{pdf_uid}",
                            "url_hash": pdf_uid,
                            "original_url": doc["original_url"],
                            "url": pdf_url,
                            "canonical_url": pdf_url,
                            "pdf_url": pdf_url,
                            "title": doc["title"] + " [PDF]",
                            "author": doc["author"],
                            "date": doc["date"],
                            "category": doc["category"],
                            "tags": doc["tags"],
                            "source": "website",
                            "source_name": "Takshashila Website",
                            "source_type": "pdf",
                            "text": "",   # extracted by src.extractors.enrich_documents_with_pdf_text
                            "content_hash": url_hash(pdf_url),
                            "local_pdf_path": str(pdf_path),
                            "extraction_method": "pymupdf",
                            "scraped_at": now_iso(),
                            "updated_at": now_iso(),
                        }
                        pdf_doc = clean_document_metadata(pdf_doc)
                        pdf_docs.append(pdf_doc)
                        existing_url_hashes.add(pdf_uid)
                        append_jsonl(config.DOCUMENTS_FILE, pdf_doc)

    summary = {
        "new_articles": len(new_docs),
        "skipped": skipped,
        "failed": failed,
        "pdf_downloaded": pdf_downloaded,
        "total_documents": len(all_docs) + len(pdf_docs),
        "discovered": len(unique_stubs),
        "discovery_counts": discovery_counts,
        "manifest_path": str(config.WEBSITE_MANIFEST_FILE),
    }
    manifest["completed_at"] = now_iso()
    manifest["summary"] = summary
    _write_manifest(manifest)

    logger.info(f"Scrape complete: {summary}")
    if progress_cb:
        progress_cb(f"✓ Scrape complete — {summary['new_articles']} new pages, "
                    f"{pdf_downloaded} PDFs downloaded, {failed} failed "
                    f"(see {config.FAILED_CSV.name})")
    return summary