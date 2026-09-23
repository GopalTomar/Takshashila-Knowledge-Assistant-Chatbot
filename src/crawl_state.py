"""
crawl_state.py — Per-source incremental crawl state.

For every URL ever ingested we remember::

    url -> {
      content_hash, etag, last_modified, sitemap_lastmod, document_id,
      first_seen, last_seen, last_changed, last_status,
      children: [urls of PDFs / op-ed entries owned by this page],
      outlinks: [internal links found last time the body was parsed],
      redirected_to: final URL if this URL redirects,
      missing_count: consecutive complete crawls in which the URL was absent/404,
      unavailable_since: first failure timestamp while temporarily failing,
    }

Why each field exists:
* ``etag`` / ``last_modified`` → conditional GET (304 = no download at all).
* ``content_hash`` → the final, authoritative change test when a body is fetched.
* ``children`` → a PDF or op-ed entry stays alive as long as its parent page is
  still seen, even when the parent is unchanged (304) and therefore not re-parsed.
* ``outlinks`` → the link graph (for orphan/broken-link audits) survives 304s.
* ``missing_count`` → removals need ``KB_REMOVAL_CONFIRMATIONS`` consecutive
  complete crawls, so a single bad day never deletes knowledge.

State lives in ``<KB_ROOT>/state/<source>_crawl_state.json`` so it travels with
the KB bundle. A legacy file in data/logs/ is migrated on first load.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from src import config
from src.utils import get_logger, now_iso

logger = get_logger("crawl_state", config.SCRAPE_LOG)


def _safe(source: str) -> str:
    return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in (source or "source"))


def _state_path(source: str) -> Path:
    return config.STATE_DIR / f"{_safe(source)}_crawl_state.json"


class CrawlState:
    """Load / query / update the incremental crawl state for one source."""

    def __init__(self, source: str, path: Optional[Path] = None):
        self.source = source
        self.path = Path(path) if path else _state_path(source)
        self._lock = threading.RLock()
        # Fingerprint of the extraction code that produced the stored documents
        # (see crawl_engine.extractor_version); "" for legacy state files.
        self.extractor_version: str = ""
        self.urls: Dict[str, Dict] = self._load()
        self._seen_this_run: Set[str] = set()

    # ── persistence ──────────────────────────────────────────────────────────────
    def _load(self) -> Dict[str, Dict]:
        candidates = [self.path]
        # Legacy location (data/logs) is migrated only for the production KB root.
        if self.path == _state_path(self.source) and config.KB_ROOT == config.DATA_DIR:
            candidates.append(config.LOGS_DIR / f"{_safe(self.source)}_crawl_state.json")
        for p in candidates:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        if p != self.path:
                            logger.info(f"Migrating legacy crawl state {p} → {self.path}")
                        if "urls" in data:
                            self.extractor_version = str(data.get("extractor_version") or "")
                        return data.get("urls", data)
                except Exception as exc:
                    logger.warning(f"Could not read crawl state {p.name}: {exc}; starting fresh.")
        return {}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"source": self.source, "saved_at": now_iso(),
                       "extractor_version": self.extractor_version, "urls": self.urls}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # ── queries ──────────────────────────────────────────────────────────────────
    def known(self, url: str) -> bool:
        return url in self.urls

    def get(self, url: str) -> Dict:
        return self.urls.get(url) or {}

    def stored_hash(self, url: str) -> Optional[str]:
        return self.get(url).get("content_hash")

    def is_unchanged(self, url: str, content_hash: str) -> bool:
        return bool(content_hash) and self.stored_hash(url) == content_hash

    def conditional_headers(self, url: str) -> Dict[str, str]:
        rec = self.get(url)
        headers: Dict[str, str] = {}
        if rec.get("etag"):
            headers["If-None-Match"] = rec["etag"]
        if rec.get("last_modified"):
            headers["If-Modified-Since"] = rec["last_modified"]
        return headers

    def document_id(self, url: str) -> Optional[str]:
        return self.get(url).get("document_id") or None

    def seen_this_run(self, url: str) -> bool:
        return url in self._seen_this_run

    # ── updates ──────────────────────────────────────────────────────────────────
    def mark_seen(self, url: str, *, with_children: bool = True) -> None:
        """URL is alive this run (fetched, 304, or temporarily failing but kept)."""
        with self._lock:
            self._seen_this_run.add(url)
            rec = self.urls.get(url)
            if rec is not None:
                rec["last_seen"] = now_iso()
                rec["missing_count"] = 0
                if with_children:
                    for c in rec.get("children") or []:
                        self._seen_this_run.add(c)
                        if c in self.urls:
                            self.urls[c]["missing_count"] = 0

    def record(self, url: str, *, content_hash: str, document_id: str, etag: str = "",
               last_modified: str = "", changed: bool = True, **extra) -> None:
        """Insert/update the record for a URL after processing it."""
        with self._lock:
            existing = self.urls.get(url, {})
            ts = now_iso()
            rec = dict(existing)
            rec.update({
                "content_hash": content_hash,
                "etag": etag or existing.get("etag", ""),
                "last_modified": last_modified or existing.get("last_modified", ""),
                "document_id": document_id or existing.get("document_id", ""),
                "first_seen": existing.get("first_seen", ts),
                "last_seen": ts,
                "last_changed": ts if changed else existing.get("last_changed", ts),
                "missing_count": 0,
            })
            rec.pop("unavailable_since", None)
            for k, v in extra.items():
                if v is not None:
                    rec[k] = v
            self.urls[url] = rec
            self._seen_this_run.add(url)

    def update_fields(self, url: str, **fields) -> None:
        with self._lock:
            if url in self.urls:
                self.urls[url].update({k: v for k, v in fields.items() if v is not None})

    def mark_unavailable(self, url: str, status) -> None:
        """Temporary failure: keep the document, remember since when it fails."""
        with self._lock:
            rec = self.urls.get(url)
            if rec is not None:
                rec.setdefault("unavailable_since", now_iso())
                rec["last_status"] = status
        self.mark_seen(url)

    def remove(self, url: str) -> None:
        with self._lock:
            self.urls.pop(url, None)

    # ── removal detection ────────────────────────────────────────────────────────
    def begin_run(self) -> None:
        self._seen_this_run = set()

    def unseen_urls(self) -> List[str]:
        return [u for u in self.urls if u not in self._seen_this_run]

    def confirm_missing(self, urls: Iterable[str], confirmations: int) -> List[str]:
        """
        Increment ``missing_count`` for URLs absent this (complete) run and return
        those that have now been missing ``confirmations`` times in a row.
        """
        confirmed = []
        with self._lock:
            for u in urls:
                rec = self.urls.get(u)
                if rec is None:
                    continue
                rec["missing_count"] = int(rec.get("missing_count", 0)) + 1
                if rec["missing_count"] >= max(1, confirmations):
                    confirmed.append(u)
        return confirmed

    def document_ids_for(self, urls: Iterable[str]) -> List[str]:
        return [d for d in (self.document_id(u) for u in urls) if d]

    def stats(self) -> Dict[str, int]:
        return {"known_urls": len(self.urls), "seen_this_run": len(self._seen_this_run)}
