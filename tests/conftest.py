"""
Shared fixtures. Tests are hermetic: no network, no LLM, no model download.

* ``fake_embeddings`` — a deterministic hashing embedder (384-dim, L2-normalised
  bag of words) patched into src.embeddings, so FAISS behaves like a lexical
  cosine search. Real ranking code (RRF, filters, boosts) runs unchanged.
* ``kb_root`` — an isolated KB root in tmp; config paths are restored afterwards.
* ``tiny_kb`` — a small KB (website publications/blog/person/op-ed + Commit KB +
  holiday list) built through the REAL pipeline (normalise → chunk → embed →
  FAISS → people graph → manifest).
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

DIM = 384
_TOK = re.compile(r"[a-z0-9]+")


def fake_embed(texts, batch_size=64, show_progress=False):
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for tok in _TOK.findall((t or "").lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            out[i, h % DIM] += 1.0
        n = np.linalg.norm(out[i])
        out[i] = out[i] / n if n else out[i]
    return out


@pytest.fixture
def fake_embeddings(monkeypatch):
    import src.embeddings as emb
    monkeypatch.setattr(emb, "embed_texts", fake_embed)
    monkeypatch.setattr(emb, "embed_query", lambda q: fake_embed([q]))
    monkeypatch.setattr(emb, "_get_model", lambda: object())
    return fake_embed


@pytest.fixture
def kb_root(tmp_path, monkeypatch):
    from src import vector_store
    original_root = config.KB_ROOT
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RELEASES_DIR", tmp_path / "releases")
    monkeypatch.setattr(config, "CURRENT_POINTER", tmp_path / "releases" / "CURRENT")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "DAILY_REPORTS_DIR", tmp_path / "reports" / "daily_refresh")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "SUPPLEMENTARY_KB_FILES", [(tmp_path / "kb_in" / "holiday_list_2026.jsonl", "local")])
    monkeypatch.delenv("KB_ROOT", raising=False)
    config.use_kb_root(tmp_path)
    vector_store.reset()
    yield tmp_path
    vector_store.reset()
    config.use_kb_root(original_root)


def make_docs():
    """Realistic documents in the crawler's output shape."""
    W = "https://takshashila.org.in"
    return [
        {"document_id": "website_pub1", "source": "website", "url": f"{W}/content/publications/geo-report.html",
         "title": "Geospatial Technology and Indian Strategy",
         "authors": ["Y Nithiyanandam"], "author_urls": [f"{W}/content/team/y-nithiyanandam.html"],
         "date": "2025-02-10", "categories": ["Geospatial", "Technology"],
         "text": "# Summary\n\nSatellite imagery and remote sensing give India new tools for border monitoring. "
                 "Geospatial intelligence supports disaster response, urban planning and climate adaptation.\n\n"
                 "## Recommendations\n\nIndia should open more geospatial data to researchers and build a "
                 "national earth observation programme with private satellite operators."},
        {"document_id": "website_blog1", "source": "website", "url": f"{W}/content/blogs/semiconductor-policy.html",
         "title": "Semiconductor Policy Needs Patience",
         "authors": ["Pranay Kotasthane"], "author_urls": [f"{W}/content/team/pranay-kotasthane.html"],
         "date": "2024-06-01", "categories": ["High Tech Geopolitics"],
         "text": "India's semiconductor subsidies will take a decade to show results. Fabrication plants "
                 "need skilled workers, water, power and a stable policy environment. Chip design firms "
                 "are India's comparative advantage in the semiconductor value chain."},
        {"document_id": "website_person1", "source": "website", "url": f"{W}/content/team/pranay-kotasthane.html",
         "title": "Pranay Kotasthane", "content_type": "person", "role": "Deputy Director",
         "research_areas": ["High Tech Geopolitics", "Public Policy"],
         "works": [{"title": "Semiconductor Policy Needs Patience", "url": f"{W}/content/blogs/semiconductor-policy.html",
                    "kind": "blog", "date": "2024-06-01"}],
         "text": "# Pranay Kotasthane\n\nRole: Deputy Director\n\nPranay Kotasthane researches high-tech "
                 "geopolitics and public policy.\n\n## Areas of research\n\n- High Tech Geopolitics\n- Public Policy"},
        {"document_id": "website_oped_1", "source": "website", "is_external_reference": True,
         "url": "https://www.example-news.com/opinion/chips-and-china", "content_type": "op-ed",
         "title": "Chips, China and India's choices", "authors": ["Pranay Kotasthane"], "date": "2025-08-01",
         "publisher": "Example News", "categories": ["Geopolitics"],
         "text": "Op-ed / media article by Takshashila authors: Chips, China and India's choices\n"
                 "Authors: Pranay Kotasthane\nPublished in: Example News\nDate: 2025-08-01"},
        {"document_id": "commit_kb_sample", "source": "commit_kb",
         "url": "https://commit.example.org/playbook/2026-01-01-sample-review-rule.html",
         "title": "Sample Review Rule",
         "text": "The sample review rule lets any staff member raise a red flag on a draft before publication. "
                 "A red flag pauses release until the programme head reviews the concern."},
    ]


def build_kb(docs=None, holidays=True):
    """Write documents.jsonl (+ holiday input) and run the real rebuild."""
    import json
    from src.incremental_index import rebuild_index
    from src.utils import save_jsonl
    save_jsonl(config.DOCUMENTS_FILE, docs if docs is not None else make_docs())
    if holidays:
        hp = config.SUPPLEMENTARY_KB_FILES[0][0]
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps({"id": "holiday_2026_001", "title": "New Year", "date": "2026-01-01",
                                  "day": "Thursday",
                                  "content": "New Year holiday is observed on Thursday, January 1, 2026."}) + "\n",
                      encoding="utf-8")
    return rebuild_index(use_cache=True)


@pytest.fixture
def tiny_kb(kb_root, fake_embeddings):
    summary = build_kb()
    return summary
