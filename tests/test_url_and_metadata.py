"""URL canonicalisation, content-type classification and metadata normalisation."""

from src.metadata import (
    classify_content_type, clean_authors, normalize_date, normalize_document,
)
from src.url_utils import canonicalize_url, is_malformed_href, same_site

B = "https://takshashila.org.in/content/blogs/x.html"


def test_canonicalize_strips_fragment_tracking_and_index():
    assert canonicalize_url("/pages/blogs/index.html#category=AI", B) == "https://takshashila.org.in/pages/blogs/"
    assert canonicalize_url("https://TAKSHASHILA.org.in:443/a//b/../c.html?utm_source=x&b=2&a=1") == \
        "https://takshashila.org.in/a/c.html?a=1&b=2"
    assert canonicalize_url("//takshashila.org.in/x.html") == "https://takshashila.org.in/x.html"


def test_canonicalize_rejects_non_http():
    for bad in ("mailto:a@b.c", "javascript:void(0)", "tel:123", "", None, "#top"):
        assert canonicalize_url(bad, B) in (None, "https://takshashila.org.in/content/blogs/x.html")
    assert canonicalize_url("mailto:a@b.c", B) is None


def test_relative_resolution():
    assert canonicalize_url("../../content/team/a-b.html", B) == "https://takshashila.org.in/content/team/a-b.html"


def test_same_site_and_malformed():
    assert same_site("https://www.takshashila.org.in/x", "takshashila.org.in")
    assert not same_site("https://evil.com/takshashila.org.in", "takshashila.org.in")
    assert is_malformed_href("htps://x.com")
    assert not is_malformed_href("/relative/ok.html")


def test_classify_content_types():
    W = "https://takshashila.org.in"
    assert classify_content_type(f"{W}/content/publications/x.html") == "publication"
    assert classify_content_type(f"{W}/content/blogs/x.html") == "blog"
    assert classify_content_type(f"{W}/content/blogs/x.html", categories=["Op-ed"]) == "op-ed"
    assert classify_content_type(f"{W}/content/team/x.html") == "person"
    assert classify_content_type(f"{W}/content/events/x.html") == "event"
    assert classify_content_type(f"{W}/pages/research-areas/geospatial-research.html") == "research_area"
    assert classify_content_type(f"{W}/pages/about.html") == "about"
    assert classify_content_type(f"{W}/content/publications/assets/a.pdf") == "pdf"
    assert classify_content_type("https://commit.example.org/playbook/x.html", "commit_kb") == "playbook"


def test_normalize_date_formats():
    assert normalize_date("2026-06-22T14:15:10.095Z") == "2026-06-22"
    assert normalize_date("November 27, 2023") == "2023-11-27"
    assert normalize_date("27 November 2023") == "2023-11-27"
    assert normalize_date("March 2026") == "2026-03"
    assert normalize_date("not a date") == ""


def test_clean_authors_repairs_legacy_pollution():
    assert clean_authors("Bharath Reddy\n\nExecutive Summary\n\nTop") == ["Bharath Reddy"]
    assert clean_authors("Transformation\nVanshika Saraf") == ["Vanshika Saraf"]
    assert clean_authors("Action Items\nContext") == []
    assert clean_authors("A. Nithiyanandam and Pranay Kotasthane") == ["A. Nithiyanandam", "Pranay Kotasthane"]
    assert clean_authors("Ananya", known_people=["Ananya"]) == ["Ananya"]


def test_normalize_document_schema_and_no_fabrication():
    d = normalize_document({"document_id": "x", "source": "website",
                            "url": "https://takshashila.org.in/content/blogs/a.html",
                            "title": "A", "text": "hello world", "local_pdf_path": "D:\\secret\\x.pdf"})
    for k in ("content_type", "publication_date", "authors", "word_count", "content_hash",
              "source_priority", "is_pdf", "internal_links", "crawl_date"):
        assert k in d
    assert d["content_type"] == "blog"
    assert d["authors"] == [] and d["publication_date"] == ""      # nothing invented
    assert d["local_pdf_path"] == "data/raw/pdfs/x.pdf"             # no absolute machine path
    assert d["word_count"] == 2


def test_external_reference_keeps_op_ed_type():
    d = normalize_document({"document_id": "o", "source": "website", "is_external_reference": True,
                            "content_type": "op-ed", "url": "https://news.example/x", "title": "t", "text": "t"})
    assert d["content_type"] == "op-ed"


def test_percent_encoding_is_one_identity():
    raw = canonicalize_url("https://takshashila.org.in/content/publications/2026-India’s-West Asian.html")
    enc = canonicalize_url("https://takshashila.org.in/content/publications/2026-India%E2%80%99s-West%20Asian.html")
    assert raw == enc and " " not in raw


def test_documents_carry_publisher_and_access():
    from src.metadata import normalize_document
    web = normalize_document({"document_id": "w", "source": "website", "title": "T", "text": "x",
                              "url": "https://takshashila.org.in/content/blogs/a.html"})
    ck = normalize_document({"document_id": "c", "source": "commit_kb", "title": "T", "text": "x",
                             "url": "https://commit.example.org/playbook/a.html"})
    oped = normalize_document({"document_id": "o", "source": "website", "is_external_reference": True,
                               "publisher": "The Hindu", "content_type": "op-ed", "title": "T",
                               "text": "x", "url": "https://news.example/a"})
    assert (web["publisher"], web["access"]) == ("Takshashila Institution", "public")
    assert (ck["publisher"], ck["access"]) == ("Takshashila Institution", "internal")
    assert (oped["publisher"], oped["access"]) == ("The Hindu", "public")
