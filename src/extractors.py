"""
extractors.py — PDF text and PDF metadata extraction (PyMuPDF).

HTML extraction lives in src/site_extract.py. The legacy helpers that guessed
categories from URL keywords and authors from free text were removed: they
produced fabricated metadata (see AUDIT_REPORT C2/C7) and were no longer called.
"""

import re
from pathlib import Path
from typing import Dict, List

from src import config
from src.utils import clean_text, get_logger

logger = get_logger("extractors", config.SCRAPE_LOG)


def extract_pdf_text(pdf_path: Path) -> List[Dict]:
    """Extract text page-by-page: [{"page_number": n, "text": ...}, …]."""
    pages = []
    try:
        import pymupdf as fitz
        doc = fitz.open(str(pdf_path))
        for page_num in range(len(doc)):
            text = clean_text(doc[page_num].get_text("text"))
            if text:
                pages.append({"page_number": page_num + 1, "text": text})
        doc.close()
        logger.info(f"Extracted {len(pages)} pages from {Path(pdf_path).name}")
    except Exception as exc:
        logger.error(f"PDF extraction failed for {pdf_path}: {exc}")
    return pages


def extract_pdf_metadata(pdf_path: Path) -> Dict:
    """
    Embedded PDF metadata (title / author / subject / keywords / dates / page count)
    exactly as stored in the file. Empty strings when absent — never guessed.
    """
    meta: Dict = {}
    try:
        import pymupdf as fitz
        with fitz.open(str(pdf_path)) as doc:
            raw = doc.metadata or {}
            meta = {
                "pdf_title": (raw.get("title") or "").strip(),
                "pdf_author": (raw.get("author") or "").strip(),
                "pdf_subject": (raw.get("subject") or "").strip(),
                "pdf_keywords": (raw.get("keywords") or "").strip(),
                "pdf_creation_date": _pdf_date(raw.get("creationDate")),
                "pdf_modified_date": _pdf_date(raw.get("modDate")),
                "pdf_page_count": doc.page_count,
            }
    except Exception as exc:
        logger.warning(f"PDF metadata read failed for {pdf_path}: {exc}")
    return meta


def _pdf_date(raw) -> str:
    """PDF dates look like 'D:20230630101500+05'30''. Return YYYY-MM-DD or ''."""
    m = re.match(r"D?:?(\d{4})(\d{2})(\d{2})", str(raw or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
