"""
Daily-refresh automation tests (spec: PHASE 19A "AUTOMATION TEST").

Runs the REAL refresh pipeline (staging → crawl → merge → incremental embed →
FAISS/BM25 → validate → smoke → atomic promotion) against the in-memory fake
website from test_crawler, with fake embeddings.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import scripts.crawl_engine as ce
from src import config, vector_store
from tests.test_crawler import FakeSite, base_routes, page, site_config

W = "https://takshashila.org.in"


@pytest.fixture
def env(kb_root, fake_embeddings, monkeypatch):
    routes = base_routes()
    monkeypatch.setattr(ce.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "SCRAPE_MAX_RPS", 0)
    monkeypatch.setattr(config, "SCRAPE_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "COMMIT_KB_USERNAME", "")          # Commit KB skipped (no creds)
    monkeypatch.setattr(ce, "website_config", lambda **kw: site_config())
    monkeypatch.setattr(ce.CrawlEngine, "_build_session", lambda self: FakeSite(routes))
    return routes


def _refresh(**kw):
    from src.refresh import run_refresh
    return run_refresh(**kw)


def _current():
    return config.CURRENT_POINTER.read_text().strip() if config.CURRENT_POINTER.exists() else ""


def _search(q):
    from src.retriever import retrieve
    vector_store.reset()
    return retrieve(q, top_k=3, state=vector_store.build_state(root=config.resolve_active_kb_root()))


def test_first_refresh_builds_and_promotes(env):
    r = _refresh(run_id="r1")
    assert r["status"] == "partial" or r["status"] == "success"      # commit_kb skipped → warning only
    assert r["promoted"] and _current() == "r1"
    assert r["website"]["new"] >= 4 and r["validation"]["status"] == "passed"
    latest = json.loads((config.DAILY_REPORTS_DIR / "latest.json").read_text())
    for k in ("run_id", "started_at", "completed_at", "duration_seconds", "timezone", "website",
              "commit_kb", "index", "validation", "totals", "errors", "warnings"):
        assert k in latest
    assert latest["timezone"] == "Asia/Kolkata"
    assert _search("satellite imagery borders")[0]["title"] == "Geospatial Report"


def test_change_is_detected_and_only_changed_chunks_embedded(env):
    _refresh(run_id="r1")
    env[f"{W}/content/blogs/chips.html"]["body"] = page(
        "Chips Blog", "Quantum photonics foundries are the new frontier for Indian chip design. " * 3,
        author="Pranay Kotasthane")
    r = _refresh(run_id="r2")
    assert r["promoted"] and _current() == "r2"
    assert r["website"]["modified"] == 1 and r["website"]["new"] == 0
    assert 0 < r["index"]["embedded"] < r["index"]["chunks"]           # unchanged chunks from cache
    assert r["index"]["cached"] > 0
    hits = _search("quantum photonics foundries")
    assert hits[0]["title"] == "Chips Blog"


def test_no_change_means_no_reembedding(env):
    _refresh(run_id="r1")
    r = _refresh(run_id="r2")
    assert r["index"]["status"] == "unchanged" and r["index"]["embedded"] == 0
    assert r["website"]["new"] == 0 and r["website"]["modified"] == 0


def test_crawl_failure_keeps_production(env):
    _refresh(run_id="r1")
    env[f"{W}/"] = {"status": 503, "body": "down"}
    r = _refresh(run_id="r2")
    assert r["status"] == "failed" and not r["promoted"]
    assert _current() == "r1"                                           # old release still active
    assert _search("satellite imagery borders")                          # and still serves results
    status = json.loads((config.DAILY_REPORTS_DIR / "status.json").read_text())
    assert status["consecutive_failures"] == 1


def test_validation_failure_is_not_promoted(env, monkeypatch):
    _refresh(run_id="r1")
    import scripts.validate_kb as vk
    monkeypatch.setattr(vk, "validate", lambda baseline=None: {
        "ok": False, "errors": ["simulated corruption"], "warnings": [], "counts": {}, "root": ""})
    env[f"{W}/content/blogs/chips.html"]["body"] = page("Chips Blog", "Changed text body " * 10)
    r = _refresh(run_id="r2")
    assert r["status"] == "failed" and not r["promoted"] and _current() == "r1"
    assert (config.RELEASES_DIR / "failed-r2").exists()
    assert "simulated corruption" in " ".join(r["errors"])


def test_repeated_failures_mark_degraded(env):
    _refresh(run_id="r1")
    env[f"{W}/"] = {"status": 503, "body": "down"}
    for i in range(3):
        _refresh(run_id=f"f{i}")
    st = json.loads((config.DAILY_REPORTS_DIR / "status.json").read_text())
    assert st["consecutive_failures"] == 3 and st["health"] == "degraded"
    assert _current() == "r1"


def test_dry_run_changes_nothing(env):
    _refresh(run_id="r1")
    env[f"{W}/content/blogs/chips.html"]["body"] = page("Chips Blog", "Changed text body " * 10)
    r = _refresh(run_id="r2", dry_run=True)
    assert r["status"] == "dry_run" and r["would_change"]["documents"] == 1
    assert _current() == "r1" and not (config.RELEASES_DIR / "r2").exists()


# ── Scheduler gate (timezone correctness) ──────────────────────────────────────────
from scripts.refresh_gate import decide  # noqa: E402


def _utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_gate_uses_ist_not_utc():
    # 06:00 IST == 00:30 UTC
    assert decide(_utc("2026-09-23T00:29:00"), "06:00", "Asia/Kolkata", True)[0] is False
    assert decide(_utc("2026-09-23T00:31:00"), "06:00", "Asia/Kolkata", True)[0] is True
    # 06:00 UTC would be 11:30 IST — must NOT be treated as the schedule
    assert decide(_utc("2026-09-22T23:00:00"), "06:00", "Asia/Kolkata", True)[0] is False


def test_gate_once_per_local_day_and_late_start():
    assert decide(_utc("2026-09-23T03:00:00"), "06:00", "Asia/Kolkata", True, "2026-09-23")[0] is False
    assert decide(_utc("2026-09-23T03:00:00"), "06:00", "Asia/Kolkata", True, "2026-09-22")[0] is True
    assert decide(_utc("2026-09-23T03:00:00"), "06:00", "Asia/Kolkata", False)[0] is False


def test_next_scheduled_run_in_ist():
    from src.refresh import next_scheduled_run
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 23, 7, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    nxt = next_scheduled_run(now)
    assert (nxt.day, nxt.hour, nxt.minute) == (24, 6, 0) and nxt.utcoffset().total_seconds() == 19800


def test_pipeline_change_triggers_reindex_without_content_change(env, monkeypatch):
    _refresh(run_id="r1")
    import src.incremental_index as ii
    monkeypatch.setattr(ii, "pipeline_version", lambda: "new-processing-code")
    r = _refresh(run_id="r2")
    assert r["pipeline_changed"] and r["index"]["status"] == "success" and r["promoted"]
    assert r["index"]["embedded"] == 0 and r["index"]["cached"] > 0        # all from cache
