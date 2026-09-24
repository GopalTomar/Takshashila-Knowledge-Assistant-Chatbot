"""Low-memory serving (Render Free): compact BM25, streaming metadata, safe KB swap."""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from src import config, vector_store


# ── BM25: identical scores to rank_bm25 ────────────────────────────────────────────
def test_compact_bm25_scores_identical_to_rank_bm25():
    rank_bm25 = pytest.importorskip("rank_bm25")
    random.seed(3)
    words = [f"w{i}" for i in range(300)] + ["common"] * 50       # "common" → negative idf floor
    corpus = [[random.choice(words) for _ in range(random.randint(1, 60))] for _ in range(400)]
    corpus.append(["_"])
    ref = rank_bm25.BM25Okapi(corpus)
    got = vector_store.CompactBM25(corpus)
    assert got.avgdl == ref.avgdl and got.average_idf == ref.average_idf
    queries = [["common"], ["w1", "w1"], ["missing"], ["w5", "common", "w7", "zzz"]] + \
              [random.sample(words, k) for k in (1, 2, 4, 8) for _ in range(25)]
    for q in queries:
        assert np.array_equal(ref.get_scores(q), got.get_scores(q)), q


# ── metadata.json streaming reader ─────────────────────────────────────────────────
def test_streaming_metadata_reader_equals_json_load(tmp_path, monkeypatch):
    items = [{"document_id": f"d{i % 3}", "url": "https://x.org/a,b]c", "title": 'q"uote ] , [',
              "authors": ["A", "B"], "text": "é ह 🙂 " * (i % 50), "n": i, "nested": {"l": [1, [2, "]"]]}}
             for i in range(500)]
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    real_iter = vector_store._iter_json_array
    monkeypatch.setattr(vector_store, "_iter_json_array", lambda path: real_iter(path, block=37))  # force splits
    got = vector_store._read_metadata(tmp_path)
    assert got == items
    # repeated values are shared (memory), content unchanged
    assert got[0]["url"] is got[1]["url"] and got[0]["authors"] is got[3]["authors"]
    (tmp_path / "metadata.json").write_text("[]", encoding="utf-8")
    assert vector_store._read_metadata(tmp_path) == []


# ── Low-memory release swap ─────────────────────────────────────────────────────────
def _second_release(kb_root, tmp_path, fake_embeddings, extra_title):
    """Build a second, different release root via the real pipeline."""
    import shutil
    from tests.conftest import build_kb, make_docs
    first = config.KB_ROOT
    rel = tmp_path / "rel2"
    shutil.copytree(first, rel)
    config.use_kb_root(rel)
    docs = make_docs() + [{"document_id": "website_new", "source": "website",
                           "url": "https://takshashila.org.in/content/blogs/new.html",
                           "title": extra_title, "text": "A brand new analysis of quantum policy. " * 10}]
    build_kb(docs)
    config.use_kb_root(first)
    vector_store.reset()
    return rel


def test_low_memory_swap_activates_new_release(tiny_kb, kb_root, tmp_path, fake_embeddings, monkeypatch):
    from src import kb_sync
    monkeypatch.setattr(config, "KB_LOW_MEMORY", True)
    rel2 = _second_release(kb_root, tmp_path, fake_embeddings, "Quantum Policy Now")
    vector_store.load_index()
    before = vector_store.get_state()
    kb_sync.activate_release(rel2)
    after = vector_store.get_state()
    assert after is not before and after.root == str(rel2)
    assert any(m.get("title") == "Quantum Policy Now" for m in after.metadata)


def test_low_memory_swap_failure_restores_previous(tiny_kb, kb_root, tmp_path, fake_embeddings, monkeypatch):
    from src import kb_sync
    monkeypatch.setattr(config, "KB_LOW_MEMORY", True)
    rel2 = _second_release(kb_root, tmp_path, fake_embeddings, "Broken Release")
    (rel2 / "index" / "metadata.json").write_text('[{"broken": ', encoding="utf-8")  # corrupt
    vector_store.load_index()
    old_root = vector_store.get_state().root
    with pytest.raises(Exception):
        kb_sync.activate_release(rel2)
    st = vector_store.get_state()                     # previous release serving again
    assert st.root == old_root and st.ntotal > 0
    assert not any(m.get("title") == "Broken Release" for m in st.metadata)


def test_low_memory_swap_rejects_incomplete_release_without_unloading(tiny_kb, kb_root, tmp_path,
                                                                      fake_embeddings, monkeypatch):
    from src import kb_sync
    monkeypatch.setattr(config, "KB_LOW_MEMORY", True)
    rel2 = _second_release(kb_root, tmp_path, fake_embeddings, "Incomplete")
    (rel2 / "index" / "faiss.index").unlink()
    vector_store.load_index()
    before = vector_store.get_state()
    with pytest.raises(ValueError):
        kb_sync.activate_release(rel2)
    assert vector_store.get_state() is before           # never released


def test_loads_refused_while_swapping(tiny_kb, kb_root):
    vector_store.suspend()
    try:
        with pytest.raises(vector_store.KBReloading):
            vector_store.get_state()
    finally:
        vector_store.resume()
    assert vector_store.get_state().ntotal > 0


def test_api_reports_503_during_swap(tiny_kb, kb_root, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as m
    monkeypatch.setitem(m._ready, "ok", True)
    vector_store.load_index()
    c = TestClient(m.app)
    assert c.get("/ready").status_code == 200
    vector_store.suspend()
    try:
        r = c.get("/ready")
        assert r.status_code == 503 and r.headers.get("retry-after") == "30"
        q = c.post("/api/query", json={"query": "semiconductor policy", "mode": "search"})
        assert q.status_code == 503
    finally:
        vector_store.resume()


# ── Precomputed BM25 artifact ──────────────────────────────────────────────────────
def test_index_build_writes_bm25_artifact_and_load_is_identical(tiny_kb, kb_root):
    idx_dir = config.KB_ROOT / "index"
    assert (idx_dir / "bm25.json").exists() and (idx_dir / "bm25.npz").exists()
    md = vector_store._read_metadata(idx_dir)
    fresh = vector_store._compute_bm25(md)
    loaded = vector_store._build_bm25(md, idx_dir)
    assert type(loaded) is vector_store.CompactBM25 and loaded is not fresh
    for q in (["semiconductor"], ["geospatial", "satellite", "india"], ["red", "flag"], ["nothere"]):
        assert np.array_equal(fresh.get_scores(q), loaded.get_scores(q))


def test_stale_bm25_artifact_is_ignored(tiny_kb, kb_root, monkeypatch):
    idx_dir = config.KB_ROOT / "index"
    md = vector_store._read_metadata(idx_dir)
    man = json.loads((idx_dir / "bm25.json").read_text(encoding="utf-8"))
    man["fingerprint"] = "0" * 64                              # e.g. metadata changed since
    (idx_dir / "bm25.json").write_text(json.dumps(man), encoding="utf-8")
    loaded = []
    monkeypatch.setattr(vector_store.CompactBM25, "load", classmethod(lambda cls, d: loaded.append(d)))
    bm = vector_store._build_bm25(md, idx_dir)
    assert not loaded and isinstance(bm, vector_store.CompactBM25)   # rebuilt, not loaded
