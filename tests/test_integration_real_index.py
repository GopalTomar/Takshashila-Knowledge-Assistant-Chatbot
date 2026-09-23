"""
Opt-in smoke tests against the REAL active KB and embedding model.

    RUN_INTEGRATION=1 pytest tests/test_integration_real_index.py -q

Skipped by default: they need the built index and the Hugging Face model.
"""

import os

import pytest

from src import config

pytestmark = pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1",
                                reason="set RUN_INTEGRATION=1 to run against the real index")


def test_index_loads_and_matches_metadata():
    from src import vector_store
    st = vector_store.load_index(force=True)
    assert st.ntotal > 1000 and st.ntotal == len(st.metadata)


@pytest.mark.parametrize("q,expected_type", [
    ("geospatial research programme", None),
    ("Pranay Kotasthane op-eds", None),
])
def test_real_retrieval(q, expected_type):
    from src.retriever import retrieve
    hits = retrieve(q, top_k=5, allowed_sources=config.INTERNAL_SOURCES)
    assert hits and all(h.get("title") and h.get("url") for h in hits if h.get("source") != "local")
