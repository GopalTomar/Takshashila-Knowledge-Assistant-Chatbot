"""
supplementary.py — Curated internal files folded into every index build.

``config.SUPPLEMENTARY_KB_FILES`` lists JSONL files maintained by hand (e.g. the
2026 holiday list). Previously config promised these were "folded into the
index if present" but nothing loaded them, so the holiday list was never
searchable. Each record becomes a normal document with ``source`` from config.

Accepted record fields: ``id``, ``title``, ``text`` or ``content``, optional
``url``, ``date``, ``category``, ``keywords``/``tags``, ``content_type``.
Records without an id or text are reported and skipped (never silently).
"""

from __future__ import annotations

from typing import Dict, List

from src import config
from src.utils import content_hash, get_logger, load_jsonl

logger = get_logger("supplementary", config.SCRAPE_LOG)


def _holiday_text(rec: Dict) -> str:
    base = (rec.get("content") or rec.get("text") or "").strip()
    bits = [base]
    if rec.get("date") and rec["date"] not in base:
        bits.append(f"Date: {rec['date']}" + (f" ({rec['day']})" if rec.get("day") else ""))
    return "\n".join(b for b in bits if b)


def load_supplementary_documents() -> List[Dict]:
    docs: List[Dict] = []
    for path, source in config.SUPPLEMENTARY_KB_FILES:
        if not path.exists():
            continue
        records = load_jsonl(path)
        skipped = 0
        is_holiday = "holiday" in path.name.lower()
        label = path.stem.replace("_", " ").title()
        for rec in records:
            rid = str(rec.get("id") or rec.get("document_id") or "").strip()
            text = _holiday_text(rec) if is_holiday else (rec.get("text") or rec.get("content") or "").strip()
            if not rid or not text:
                skipped += 1
                continue
            title = (rec.get("title") or rid).strip()
            if is_holiday and rec.get("date"):
                title = f"{title} — holiday on {rec['date']}"
            docs.append({
                "document_id": f"{source}_{rid}",
                "source": source,
                "source_name": f"Takshashila {label}",
                "title": title,
                "text": text,
                "url": rec.get("url", ""),
                "date": rec.get("date", ""),
                "category": rec.get("category") or ("Holidays" if is_holiday else ""),
                "tags": rec.get("tags") or rec.get("keywords") or [],
                "content_type": rec.get("content_type") or ("holiday" if is_holiday else "document"),
                "source_file": path.name,
                "content_hash": content_hash(text),
            })
        if skipped:
            logger.warning(f"{path.name}: skipped {skipped} record(s) without id/text")
        logger.info(f"supplementary {path.name}: {len(records) - skipped} document(s)")
    return docs
