"""
people.py — Person ↔ content relationship graph.

Built from the documents themselves after every ingest (so a new article or a
changed profile updates the graph on the next refresh):

    person
      ├─ profile_url, role, bio, research_areas     (from /content/team/<slug>.html)
      └─ works: [{document_id, title, url, content_type, date}]
                 from (a) article bylines (authors + author profile links) and
                      (b) the works listed on the person's own profile page

Stored at ``config.PEOPLE_FILE``. The retriever uses :func:`detect_people` to
recognise a person named in a question ("What has X written about AI?") and
boosts that person's documents, so author questions retrieve the right items.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Dict, Iterable, List, Optional

from src import config
from src.metadata import slugify
from src.utils import get_logger

logger = get_logger("people", config.SCRAPE_LOG)


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def build_people_graph(docs: Iterable[Dict]) -> Dict[str, Dict]:
    people: Dict[str, Dict] = {}

    def person(name: str) -> Dict:
        k = _key(name)
        if k not in people:
            people[k] = {"name": name.strip(), "slug": slugify(name), "profile_url": "",
                         "profile_document_id": "", "role": "", "bio": "",
                         "research_areas": [], "works": []}
        return people[k]

    def add_work(p: Dict, work: Dict) -> None:
        key = work.get("url") or work.get("document_id") or work.get("title")
        if key and all((w.get("url") or w.get("document_id") or w.get("title")) != key
                       for w in p["works"]):
            p["works"].append(work)

    docs = list(docs)
    for d in docs:                                  # profiles first
        if d.get("content_type") == "person" and d.get("title"):
            p = person(d["title"])
            p.update({"profile_url": d.get("url", ""), "profile_document_id": d.get("document_id", ""),
                      "role": d.get("role") or p["role"],
                      "bio": (d.get("bio") or "")[:1500],
                      "research_areas": list(d.get("research_areas") or [])})
            for w in d.get("works") or []:
                add_work(p, {"title": w.get("title", ""), "url": w.get("url", ""),
                             "content_type": {"in_the_news": "op-ed"}.get(w.get("kind"), w.get("kind", "")),
                             "date": w.get("date", ""), "via": "profile"})
    # Byline links often point at deleted profiles (404 on the live site), so a
    # byline URL is only trusted when that profile page is actually in the KB.
    live_profiles = {d.get("url") for d in docs if d.get("content_type") == "person"}
    for d in docs:                                  # bylines
        if d.get("content_type") == "person":
            continue
        urls = list(d.get("author_urls") or [])
        for i, name in enumerate(d.get("authors") or []):
            p = person(name)
            if not p["profile_url"] and i < len(urls) and urls[i] in live_profiles:
                p["profile_url"] = urls[i]
            add_work(p, {"document_id": d.get("document_id", ""), "title": d.get("title", ""),
                         "url": d.get("url", ""), "content_type": d.get("content_type", ""),
                         "date": d.get("publication_date") or d.get("date", ""), "via": "byline"})
    for p in people.values():
        p["works"].sort(key=lambda w: w.get("date") or "", reverse=True)
        p["work_count"] = len(p["works"])
    return dict(sorted(people.items()))


def save_people_graph(people: Dict[str, Dict], path=None) -> None:
    path = path or config.PEOPLE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(people, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    logger.info(f"people graph: {len(people)} people written to {path.name}")


# ── Query-time lookup ────────────────────────────────────────────────────────────
_CACHE = {"path": None, "mtime": None, "people": {}, "index": {}}
_LOCK = threading.Lock()


def _load() -> Dict:
    path = config.PEOPLE_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"people": {}, "index": {}}
    with _LOCK:
        if _CACHE["path"] != str(path) or _CACHE["mtime"] != mtime:
            try:
                people = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"people graph unreadable: {exc}")
                people = {}
            index: Dict[str, List[str]] = {}
            for k, p in people.items():
                toks = _key(p["name"]).split()
                index.setdefault(" ".join(toks), []).append(k)          # full name
                if len(toks) >= 2:
                    index.setdefault(toks[-1], []).append(k)            # surname
            _CACHE.update(path=str(path), mtime=mtime, people=people, index=index)
        return _CACHE


def get_people() -> Dict[str, Dict]:
    return _load()["people"]


def detect_people(query: str) -> List[Dict]:
    """People explicitly named in ``query`` (full name, or an unambiguous surname)."""
    data = _load()
    if not data["people"]:
        return []
    q = " " + re.sub(r"[^\w\s]", " ", (query or "").lower()) + " "
    q = re.sub(r"\s+", " ", q)
    found: List[str] = []
    for phrase, keys in sorted(data["index"].items(), key=lambda kv: -len(kv[0])):
        if len(phrase) < 4 or f" {phrase} " not in q:
            continue
        if " " not in phrase and len(keys) != 1:
            continue                                    # ambiguous surname
        for k in keys:
            # A surname hit must not duplicate a person already matched by full name.
            if k not in found:
                found.append(k)
    return [data["people"][k] for k in found]


def find_person(name_or_slug: str) -> Optional[Dict]:
    people = get_people()
    k = _key(name_or_slug)
    if k in people:
        return people[k]
    for p in people.values():
        if p.get("slug") == name_or_slug:
            return p
    return None
