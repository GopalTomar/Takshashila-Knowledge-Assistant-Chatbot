"""
url_utils.py — URL canonicalisation and classification for the crawler.

A page must have exactly ONE identity no matter how it was linked, otherwise the
same article is fetched, stored and cited several times. ``canonicalize_url``
is that identity function:

* resolves relative links against a base URL and protocol-relative ``//`` links;
* lower-cases scheme and host, drops default ports and ``www.``-less/with mismatch
  is left alone (the host is kept as served);
* removes ``#fragments`` (same document) — the old crawler instead refused to
  follow ANY link containing ``#`` and so missed e.g. ``/pages/blogs/#category=AI``
  style links' target pages;
* removes tracking / session query parameters (utm_*, fbclid, gclid, share, …) and
  sorts the rest so ``?a=1&b=2`` == ``?b=2&a=1``;
* collapses duplicate slashes and resolves ``.``/``..`` path segments;
* treats a trailing ``/index.html`` as the directory URL (Quarto serves both).

Everything here is dependency-free and deterministic, so it is cheap to unit test.
"""

from __future__ import annotations

import posixpath
import re
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

# Query parameters that never change page content (tracking / sharing / sessions).
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref",
    "ref_src", "share", "replytocom", "amp", "_ga", "_gl", "yclid", "spm",
    "sessionid", "phpsessid", "sid",
}
_TRACKING_PREFIXES = ("utm_", "pk_", "hsa_", "mtm_")

_NON_HTTP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "ftp:", "file:")

# Extensions that are documents we want (PDF) vs. assets we never fetch as pages.
DOCUMENT_EXTENSIONS = (".pdf",)
ASSET_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".css", ".js", ".mjs", ".map", ".json", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".mp3", ".m4a", ".wav", ".webm", ".mov", ".avi", ".zip", ".gz", ".tar",
    ".rar", ".7z", ".xml", ".rss", ".atom", ".csv", ".xlsx", ".xls", ".pptx", ".ppt",
    ".docx", ".doc", ".epub",
)


def is_http_url(url: str) -> bool:
    return bool(url) and url.strip().lower().startswith(("http://", "https://"))


def is_non_http_link(href: str) -> bool:
    """mailto:/tel:/javascript: etc. — never fetched, never counted as broken."""
    return (href or "").strip().lower().startswith(_NON_HTTP_SCHEMES)


def _clean_query(query: str) -> str:
    if not query:
        return ""
    kept = []
    for k, v in parse_qsl(query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_PARAMS or kl.startswith(_TRACKING_PREFIXES):
            continue
        kept.append((k, v))
    kept.sort()
    return urlencode(kept, doseq=True)


def canonicalize_url(url: str, base: str = "") -> Optional[str]:
    """
    Return the canonical form of ``url`` (resolved against ``base``), or ``None``
    for links that are not fetchable http(s) URLs (mailto:, javascript:, empty…).
    """
    if url is None:
        return None
    url = url.strip()
    if not url or is_non_http_link(url):
        return None
    if url.startswith("//"):
        url = "https:" + url
    if base and not is_http_url(url):
        url = urljoin(base, url)
    if not is_http_url(url):
        return None

    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return None
    port = parts.port if parts.port else None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    # Resolve ./ and ../ while keeping a trailing slash if present.
    trailing = path.endswith("/")
    norm = posixpath.normpath(path)
    if norm in (".", ""):
        norm = "/"
    if not norm.startswith("/"):
        norm = "/" + norm
    if trailing and not norm.endswith("/"):
        norm += "/"
    path = norm
    # /section/index.html  ≡  /section/
    if path.lower().endswith("/index.html") or path.lower().endswith("/index.htm"):
        path = path[: path.lower().rfind("index.htm")]
    # One encoding per path: "/a b’.html" and "/a%20b%E2%80%99.html" are the same page
    # (sitemaps list raw spaces/Unicode; HTTP clients send percent-encoded bytes).
    path = quote(unquote(path), safe="/:@!$&'()*+,;=-._~")

    return urlunsplit((scheme, netloc, path, _clean_query(parts.query), ""))


def same_site(url: str, domain: str) -> bool:
    """True when ``url``'s host is ``domain`` or a ``www.`` variant of it."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    d = (domain or "").lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return bool(host) and strip(host) == strip(d)


def url_extension(url: str) -> str:
    try:
        return posixpath.splitext(urlsplit(url).path)[1].lower()
    except ValueError:
        return ""


def is_pdf_url(url: str) -> bool:
    return url_extension(url) == ".pdf"


def is_asset_url(url: str) -> bool:
    return url_extension(url) in ASSET_EXTENSIONS


def url_path(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def is_malformed_href(href: str) -> bool:
    """Links that can never resolve (spaces in host, bad scheme, empty host…)."""
    h = (href or "").strip()
    if not h or is_non_http_link(h) or h.startswith("#"):
        return False
    if re.match(r"^[a-z][a-z0-9+.-]*:", h, re.I) and not is_http_url(h):
        return True  # unknown scheme like "htps:" or "ww:"
    if is_http_url(h):
        try:
            p = urlsplit(h)
            if not p.hostname or " " in p.netloc or ".." in (p.hostname or ""):
                return True
        except ValueError:
            return True
    return False
