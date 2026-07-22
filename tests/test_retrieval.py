"""
tests/test_retrieval.py — Basic retrieval smoke tests (requires built index).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config


def test_index_exists():
    """Check that FAISS index file exists."""
    assert config.FAISS_INDEX.exists(), (
        f"FAISS index not found at {config.FAISS_INDEX}. "
        "Run: python scripts/build_index.py"
    )


def test_load_index():
    """Smoke test: load index without error."""
    from src.vector_store import load_index, index_stats
    load_index(force=True)
    stats = index_stats()
    assert stats["total_chunks"] > 0, "Index is empty"
    print(f"  Index stats: {stats}")


def test_search_returns_results():
    """Run a sample search and verify results structure."""
    from src.vector_store import search
    results = search("AI governance India policy", top_k=3)
    assert len(results) > 0, "No results returned"
    for r in results:
        assert "text"  in r
        assert "title" in r
        assert "score" in r
        assert r["score"] > 0


def test_retrieve_hybrid():
    """Test hybrid BM25+FAISS retrieval."""
    from src.retriever import retrieve
    results = retrieve("geospatial policy remote sensing", top_k=5)
    assert isinstance(results, list)
    print(f"  Hybrid retrieve returned {len(results)} results")


if __name__ == "__main__":
    try:
        test_index_exists()
        print("✓ test_index_exists")
        test_load_index()
        print("✓ test_load_index")
        test_search_returns_results()
        print("✓ test_search_returns_results")
        test_retrieve_hybrid()
        print("✓ test_retrieve_hybrid")
        print("\nAll retrieval tests passed.")
    except AssertionError as e:
        print(f"✗ Test failed: {e}")
        sys.exit(1)
