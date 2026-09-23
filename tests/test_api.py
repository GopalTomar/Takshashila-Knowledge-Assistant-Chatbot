"""Production API: health, validation, access scopes, errors, rate limits (LLM stubbed)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config


@pytest.fixture
def client(tiny_kb, monkeypatch):
    import api.main as m
    import src.groq_client as gc
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(config, "API_ACCESS_TOKENS", ["staff-token-123"])
    monkeypatch.setattr(config, "PUBLIC_SOURCES", ["website"])
    monkeypatch.setattr(config, "API_RATE_LIMIT_PER_MINUTE", 1000)
    # The hashing fake embedder yields lower cosines than bge-small; scale the gates.
    monkeypatch.setattr(config, "MIN_SCORE_THRESHOLD", 0.08)
    monkeypatch.setattr(config, "CONF_MEDIUM_THRESHOLD", 0.25)
    monkeypatch.setattr(config, "CONF_HIGH_THRESHOLD", 0.4)
    from src import vector_store
    vector_store.load_index()
    monkeypatch.setattr(m, "_warm", lambda: m._ready.update(ok=True, error=None, warm_seconds=0.0))
    m._hits.clear()

    import re as _re

    def _src_num(prompt, needle):
        """Number of the context passage that contains ``needle`` (like a faithful LLM)."""
        for block in prompt.split("\n\n---\n\n"):
            m = _re.search(r"\[Source (\d+)\]", block)
            if m and needle in block:
                return m.group(1)
        return None

    def fake_generate(system_prompt, user_prompt, **kw):
        n = _src_num(user_prompt, "red flag")
        if n:
            return ("Any staff member can raise a red flag on a draft before publication, "
                    f"which pauses release until the programme head reviews it [Source {n}].")
        n = _src_num(user_prompt, "Satellite imagery")
        if n:
            return f"Satellite imagery and remote sensing give India new tools for border monitoring [Source {n}]."
        return "INSUFFICIENT_EVIDENCE"
    monkeypatch.setattr(gc, "generate", fake_generate)
    with TestClient(m.app) as c:
        m._ready.update(ok=True, error=None)
        yield c


def test_health_endpoints(client):
    assert client.get("/health").json()["status"] == "ok"
    h = client.get("/api/health").json()
    assert h["ready"] is True and h["kb"]["loaded"] and h["kb"]["vectors"] > 0
    assert "refresh" in h and h["llm_configured"] is True


def test_query_answer_with_verified_citations(client):
    r = client.post("/api/query", json={"query": "How does satellite imagery help India?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "border monitoring" in body["answer"]
    assert body["citations"] and body["citations"][0]["url"].startswith("https://takshashila.org.in/")
    assert body["citations"] == body["sources"]
    assert body["metadata"]["scope"] == "public" and body["metadata"]["request_id"]
    assert r.headers["X-Request-ID"] == body["metadata"]["request_id"]


def test_public_scope_cannot_see_commit_kb(client):
    r = client.post("/api/query", json={"query": "What is the sample review rule for drafts?"})
    body = r.json()
    assert all(c["source"] != "commit_kb" for c in body["citations"])


def test_staff_token_unlocks_internal(client):
    r = client.post("/api/query", json={"query": "What is the sample review rule for drafts?"},
                    headers={"Authorization": "Bearer staff-token-123"})
    body = r.json()
    assert body["metadata"]["scope"] == "internal"
    assert body["citations"] and body["citations"][0]["source"] == "commit_kb"
    assert body["citations"][0]["access"] == "internal"


def test_invalid_token_rejected(client):
    r = client.post("/api/query", json={"query": "anything here"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"] == "http_error"


def test_validation_errors_are_structured(client):
    r = client.post("/api/query", json={"query": "x"})
    assert r.status_code == 422
    j = r.json()
    assert j["error"] == "invalid_request" and "request_id" in j and "Traceback" not in r.text
    assert client.post("/api/query", json={"query": "valid question", "mode": "bogus"}).status_code == 422


def test_insufficient_evidence(client):
    r = client.post("/api/query", json={"query": "What is the capital of Atlantis in 1200 BC?"})
    j = r.json()
    assert j["confidence"] == "none" and j["citations"] == [] and not j["grounded"]
    assert "sufficient evidence" in j["answer"]


def test_search_mode_no_llm(client, monkeypatch):
    import src.groq_client as gc
    monkeypatch.setattr(gc, "generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
    j = client.post("/api/query", json={"query": "semiconductor policy", "mode": "search"}).json()
    assert j["citations"] and j["metadata"]["mode"] == "search"


def test_llm_failure_is_502_without_leak(client, monkeypatch):
    import src.groq_client as gc
    def boom(*a, **k):
        raise RuntimeError("secret internal detail /home/app")
    monkeypatch.setattr(gc, "generate", boom)
    r = client.post("/api/query", json={"query": "How does satellite imagery help India?"})
    assert r.status_code == 502 and "secret internal detail" not in r.text


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(config, "API_RATE_LIMIT_PER_MINUTE", 2)
    codes = [client.post("/api/query", json={"query": "semiconductor policy", "mode": "search"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limit_not_bypassed_by_spoofed_forwarded_for(client, monkeypatch):
    monkeypatch.setattr(config, "API_RATE_LIMIT_PER_MINUTE", 2)
    # The client forges a fresh left-most address each time; the proxy-appended
    # right-most address stays the same, so the limit still applies.
    codes = [client.post("/api/query", json={"query": "semiconductor policy", "mode": "search"},
                         headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]


def test_not_ready_returns_503(client):
    import api.main as m
    m._ready["ok"] = False
    try:
        assert client.post("/api/query", json={"query": "anything here"}).status_code == 503
    finally:
        m._ready["ok"] = True


def test_sources_and_people_endpoints_respect_scope(client):
    assert client.get("/api/sources/commit_kb_sample").status_code == 404
    ok = client.get("/api/sources/commit_kb_sample", headers={"Authorization": "Bearer staff-token-123"})
    assert ok.status_code == 200 and ok.json()["title"] == "Sample Review Rule"
    p = client.get("/api/people/Pranay Kotasthane").json()
    assert p["role"] == "Deputy Director" and p["work_count"] >= 2


def test_stats_public_hides_internal_counts(client):
    s = client.get("/api/stats").json()
    assert s["scope"] == "public" and "by_source" not in s
    assert client.get("/api/refresh-status").status_code == 401


def test_mattermost_routes_mounted(client):
    r = client.post("/mattermost/ask", data={"token": "wrong", "text": "hi"})
    assert r.status_code == 403


def test_llm_rate_limit_is_503_with_retry_after(client, monkeypatch):
    import src.groq_client as gc

    class RateLimitError(Exception):
        pass

    def limited(*a, **k):
        raise RateLimitError("429 from provider org_secret_id")
    monkeypatch.setattr(gc, "generate", limited)
    r = client.post("/api/query", json={"query": "How does satellite imagery help India?"})
    assert r.status_code == 503 and r.headers.get("Retry-After") == "60"
    assert "org_secret_id" not in r.text


def test_ready_and_rag_status(client):
    import api.main as m
    assert client.get("/ready").status_code == 200
    s = client.get("/rag/status").json()
    assert s["kb"]["loaded"] and "kb_sync" in s and "refresh" in s
    m._ready["ok"] = False
    try:
        assert client.get("/ready").status_code == 503
    finally:
        m._ready["ok"] = True


def test_cors_only_allows_configured_origin(tiny_kb, monkeypatch):
    import importlib
    import api.main as m
    monkeypatch.setattr(config, "CORS_ALLOW_ORIGINS", ["https://gopaltomar.github.io"])
    m2 = importlib.reload(m)
    try:
        with TestClient(m2.app) as c:
            ok = c.options("/api/query", headers={"Origin": "https://gopaltomar.github.io",
                                                  "Access-Control-Request-Method": "POST"})
            bad = c.options("/api/query", headers={"Origin": "https://evil.example",
                                                   "Access-Control-Request-Method": "POST"})
            assert ok.headers.get("access-control-allow-origin") == "https://gopaltomar.github.io"
            assert "access-control-allow-origin" not in bad.headers
    finally:
        monkeypatch.setattr(config, "CORS_ALLOW_ORIGINS", [])
        importlib.reload(m)


def test_cors_origin_setting_accepts_a_project_pages_url(monkeypatch):
    import importlib
    monkeypatch.setenv("CORS_ALLOW_ORIGINS",
                       "https://GopalTomar.github.io/Takshashila-Knowledge-Assistant-Chatbot/, https://gopaltomar.github.io")
    try:
        importlib.reload(config)
        assert config.CORS_ALLOW_ORIGINS == ["https://gopaltomar.github.io"]
    finally:
        monkeypatch.delenv("CORS_ALLOW_ORIGINS")
        importlib.reload(config)
