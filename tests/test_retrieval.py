"""Hybrid retrieval on a tiny KB built by the real pipeline (fake embeddings)."""

from src import vector_store
from src.retriever import confidence_level, retrieve
from src.vector_store import bm25_tokenize


def test_bm25_tokenizer_strips_punctuation():
    assert bm25_tokenize("AI, semiconductors; India's policy!") == ["ai", "semiconductors", "india", "s", "policy"]


def test_kb_built_with_all_sources(tiny_kb):
    st = vector_store.load_index()
    assert tiny_kb["by_source"].keys() >= {"website", "commit_kb", "local"}
    assert st.bm25 is not None and st.ntotal == len(st.metadata)
    ids = {m["document_id"] for m in st.metadata}
    assert "local_holiday_2026_001" in ids                          # holiday list is indexed




def test_every_chunk_carries_an_access_marker(tiny_kb):
    st = vector_store.load_index()
    for m in st.metadata:
        assert m["access"] == ("internal" if m["source"] == "commit_kb" else "public")
def test_semantic_and_lexical_hits(tiny_kb):
    hits = retrieve("satellite imagery geospatial border monitoring", top_k=3)
    assert hits and hits[0]["document_id"] == "website_pub1"
    assert all("score" in h and "rrf_score" in h for h in hits)


def test_access_scope_excludes_internal(tiny_kb):
    hits = retrieve("sample review rule red flag draft", top_k=5, allowed_sources=["website"])
    assert all(h["source"] == "website" for h in hits)
    internal = retrieve("sample review rule red flag draft", top_k=5, allowed_sources=["website", "commit_kb"])
    assert internal[0]["document_id"] == "commit_kb_sample"


def test_explicit_source_outside_scope_returns_nothing(tiny_kb):
    assert retrieve("sample review rule", source="commit_kb", allowed_sources=["website"]) == []


def test_filters_apply_to_bm25_and_faiss(tiny_kb):
    hits = retrieve("India policy", top_k=5, content_type="blog")
    assert hits and all(h["content_type"] == "blog" for h in hits)
    hits = retrieve("India", top_k=5, year="2025")
    assert hits and all(str(h.get("date", "")).startswith("2025") for h in hits)


def test_author_questions_surface_that_persons_work(tiny_kb):
    hits = retrieve("What has Pranay Kotasthane written?", top_k=4)
    authored = [h for h in hits if "Pranay Kotasthane" in (h.get("authors") or []) or h["content_type"] == "person"]
    assert len(authored) >= 2
    assert {h["content_type"] for h in authored} & {"blog", "op-ed", "person"}


def test_exact_title_boost(tiny_kb):
    hits = retrieve("Semiconductor Policy Needs Patience", top_k=3)
    assert hits[0]["document_id"] == "website_blog1"


def test_document_diversity_and_determinism(tiny_kb):
    a = retrieve("India geospatial satellites policy semiconductor", top_k=5)
    b = retrieve("India geospatial satellites policy semiconductor", top_k=5)
    assert [h["chunk_id"] for h in a] == [h["chunk_id"] for h in b]
    per_doc = {}
    for h in a:
        per_doc[h["document_id"]] = per_doc.get(h["document_id"], 0) + 1
    assert max(per_doc.values()) <= 2


def test_confidence_tiers():
    assert confidence_level([{"score": 0.9}]) == "high"
    assert confidence_level([{"score": 0.1}]) == "none"


def test_empty_query():
    assert retrieve("   ") == []


def test_query_intent_types():
    from src.retriever import query_intent_types
    assert "research_area" in query_intent_types("What are Takshashila's research areas?")
    assert "op-ed" in query_intent_types("Which op-eds has she written?")
    assert query_intent_types("semiconductor subsidies") == set()


def test_intent_boost_surfaces_matching_type(tiny_kb):
    hits = retrieve("Which op-eds discuss chips and China?", top_k=3)
    assert hits[0]["content_type"] == "op-ed"


def test_works_by_person_includes_profile_list(tiny_kb):
    hits = retrieve("Which blogs has Pranay Kotasthane written?", top_k=3)
    assert any(h["content_type"] == "person" and "Pranay" in h["title"] for h in hits)
