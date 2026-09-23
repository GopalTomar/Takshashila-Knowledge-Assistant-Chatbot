"""Chunking: section awareness, metadata propagation, stable IDs, quality filtering."""

from src.chunker import chunk_document, chunk_documents
from src.utils import chunk_search_text

DOC = {
    "document_id": "website_x", "source": "website", "title": "Geopolitics of AI",
    "url": "https://takshashila.org.in/content/publications/ai.html", "content_type": "publication",
    "authors": ["Pranay Kotasthane"], "author_urls": ["https://takshashila.org.in/content/team/pk.html"],
    "publication_date": "2024-03-01", "date": "2024-03-01", "categories": ["High Tech Geopolitics"],
    "text": "# Summary\n\n" + "India needs a coherent national AI strategy balancing innovation and safety. " * 20
            + "\n\n## Compute\n\n" + "Without compute India risks falling behind in AI capabilities. " * 20,
}


def test_sections_and_heading_paths():
    chunks = chunk_document(DOC)
    paths = [c["heading_path"] for c in chunks]
    assert paths[0] == "Summary" and any(p == "Summary > Compute" for p in paths)
    assert not any("Without compute" in c["text"] for c in chunks if c["heading_path"] == "Summary")


def test_metadata_propagates_to_every_chunk():
    for c in chunk_document(DOC):
        assert c["document_id"] == "website_x" and c["url"] == DOC["url"]
        assert c["authors"] == ["Pranay Kotasthane"] and c["content_type"] == "publication"
        assert c["publication_date"] == "2024-03-01" and c["categories"] == ["High Tech Geopolitics"]
        assert "Author: Pranay Kotasthane" in chunk_search_text(c)          # metadata is searchable


def test_stable_ids_and_sizes():
    a, b = chunk_document(DOC), chunk_document(DOC)
    assert [c["chunk_id"] for c in a] == [c["chunk_id"] for c in b]
    assert [c["chunk_hash"] for c in a] == [c["chunk_hash"] for c in b]
    assert all(len(c["text"]) <= 1100 for c in a)
    assert len({c["chunk_id"] for c in a}) == len(a)


def test_short_evidence_types_are_kept():
    oped = {"document_id": "o", "source": "website", "title": "Op-ed", "content_type": "op-ed",
            "url": "https://news.example/x", "text": "Op-ed by X in The Hindu."}
    assert len(chunk_document(oped)) == 1


def test_ocr_garbage_lines_dropped_but_text_kept():
    doc = dict(DOC, text="Valid sentence about India's policy choices and trade.\nÕ6ÖiÀÁ¬É ÅרÙÚÛmÇ×iÜÓ2ÝÞßà\n"
                    "Another valid line about semiconductors and fabs.")
    text = " ".join(c["text"] for c in chunk_document(doc))
    assert "Valid sentence" in text and "ÅרÙÚÛ" not in text


def test_pdf_pages_tagged():
    pdf = {"document_id": "p", "source": "website", "title": "Report [PDF]", "url": "https://x/a.pdf",
           "content_type": "pdf", "text": "p1 p2",
           "pdf_pages": [{"page_number": 1, "text": "First page text about geospatial data. " * 5},
                         {"page_number": 2, "text": "Second page text about satellites. " * 5}]}
    chunks = chunk_document(pdf)
    assert {c["page_number"] for c in chunks} == {1, 2}


def test_duplicate_chunks_deduplicated_across_documents():
    d2 = dict(DOC, document_id="website_y")
    out = chunk_documents([DOC, d2])
    assert len({c["chunk_hash"] for c in out}) == len(out)


def test_cross_document_boilerplate_removed_except_about_pages():
    from src.chunker import BOILERPLATE_MIN_DOCS
    boiler = "The Takshashila Institution is an independent centre for research and education"
    docs = [{"document_id": f"d{i}", "source": "website", "title": f"Paper {i}", "url": f"https://x/{i}",
             "content_type": "pdf", "text": f"Unique finding number {i} about fiscal policy in Indian states and their borrowing limits.\n{boiler}"}
            for i in range(BOILERPLATE_MIN_DOCS)]
    docs.append({"document_id": "about", "source": "website", "title": "About", "url": "https://x/about",
                 "content_type": "about", "text": f"{boiler}\nWe work on public policy."})
    chunks = chunk_documents(docs)
    pdf_text = " ".join(c["text"] for c in chunks if c["document_id"] != "about")
    about_text = " ".join(c["text"] for c in chunks if c["document_id"] == "about")
    assert "independent centre" not in pdf_text and "Unique finding number 3" in pdf_text
    assert "independent centre" in about_text
