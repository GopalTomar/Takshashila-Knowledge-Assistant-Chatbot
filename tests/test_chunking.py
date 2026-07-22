"""
tests/test_chunking.py — Unit tests for chunking logic
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.chunker import _split_into_paragraphs, _chunk_paragraphs, chunk_document
from src.utils import approx_token_count


SAMPLE_TEXT = """
Takshashila Institution has been at the forefront of research on Indian foreign policy.

The institution publishes regular briefs on topics ranging from geopolitics to technology governance.

In 2024, Takshashila released an extensive report on the geopolitics of artificial intelligence,
arguing that India must develop a coherent national AI strategy that balances innovation with safety.

The report identified three pillars: compute access, data governance, and international partnerships.

Without adequate compute, India risks falling behind China and the United States in AI capabilities.
Data governance frameworks must balance openness with privacy concerns, particularly in the context
of cross-border data flows under DPDP Act 2023.

International partnerships with QUAD countries and the EU could help India secure access to
advanced semiconductor technology currently controlled by a small number of firms.
"""


def test_split_paragraphs():
    paras = _split_into_paragraphs(SAMPLE_TEXT)
    assert len(paras) >= 4, f"Expected ≥4 paragraphs, got {len(paras)}"
    for p in paras:
        assert len(p) > 10, f"Paragraph too short: {repr(p)}"


def test_chunk_size_respected():
    paras = _split_into_paragraphs(SAMPLE_TEXT * 10)  # make it longer
    chunks = _chunk_paragraphs(paras, max_tokens=500, overlap_tokens=50)
    for ch in chunks:
        assert approx_token_count(ch) <= 600, (
            f"Chunk exceeds max size: {approx_token_count(ch)} tokens"
        )


def test_chunk_document_metadata():
    doc = {
        "url_hash":      "abc123",
        "title":         "Test Publication",
        "author":        "Test Author",
        "date":          "2024-01-01",
        "original_url":  "https://example.com/test",
        "pdf_url":       "",
        "source_type":   "publication",
        "category":      "AI",
        "tags":          ["ai", "policy"],
        "text":          SAMPLE_TEXT,
    }
    chunks = chunk_document(doc)
    assert len(chunks) >= 1, "Expected at least one chunk"
    for ch in chunks:
        assert ch["title"] == "Test Publication"
        assert ch["author"] == "Test Author"
        assert ch["source_type"] == "publication"
        assert "chunk_id" in ch
        assert "chunk_hash" in ch
        assert len(ch["text"]) > 10


def test_pdf_chunking_with_pages():
    doc = {
        "url_hash":    "pdf123",
        "title":       "PDF Report",
        "author":      "Jane Smith",
        "date":        "2023-06-01",
        "original_url": "https://example.com/report",
        "pdf_url":     "https://example.com/report.pdf",
        "source_type": "pdf",
        "category":    "",
        "tags":        [],
        "text":        "",  # PDFs use pdf_pages
        "pdf_pages":   [
            {"page_number": 1, "text": SAMPLE_TEXT},
            {"page_number": 2, "text": "Another page of content. " * 50},
        ],
    }
    chunks = chunk_document(doc)
    assert any(ch.get("page_number") == 1 for ch in chunks)
    assert any(ch.get("page_number") == 2 for ch in chunks)


if __name__ == "__main__":
    test_split_paragraphs()
    print("✓ test_split_paragraphs")
    test_chunk_size_respected()
    print("✓ test_chunk_size_respected")
    test_chunk_document_metadata()
    print("✓ test_chunk_document_metadata")
    test_pdf_chunking_with_pages()
    print("✓ test_pdf_chunking_with_pages")
    print("\nAll chunking tests passed.")
