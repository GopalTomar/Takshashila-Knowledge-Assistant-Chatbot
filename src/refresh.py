"""
refresh.py — The single knowledge-base refresh implementation.

Used by the scheduled GitHub Actions job, ``scripts/refresh_kb.py`` (manual),
the self-hosted scheduler and the Streamlit admin tab. Flow::

    active release (data/releases/<CURRENT> or legacy data/)
        │ copy
        ▼
    staging release  data/releases/<run_id>/        ← all work happens here
        │ crawl website + Commit KB (incremental; per-source failures isolated)
        │ merge delta → documents.jsonl
        │ re-chunk; embed ONLY new/changed chunks (embedding cache)
        │ build FAISS + metadata + people graph + manifest
        │ validate (incl. regression guards vs the active release)
        │ smoke tests (load state, BM25, retrieval returns results)
        ▼
    PASS → atomically replace data/releases/CURRENT (one os.replace)
    FAIL → staging renamed failed-<run_id>; CURRENT untouched; failure recorded

A report is written for every run to data/reports/daily_refresh/latest.json and
data/reports/daily_refresh/<YYYY-MM-DD>.json (IST date), and status.json keeps
the consecutive-failure count (health "degraded" after KB_REFRESH_DEGRADED_AFTER).
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from src import config
from src.utils import force_rmtree, get_logger

logger = get_logger("refresh", config.SCRAPE_LOG)

KB_SUBDIRS = ("processed", "index", "state")
KEEP_RELEASES = 3
SMOKE_QUERIES = ["Takshashila Institution", "public policy research India"]


def _tz() -> ZoneInfo:
    return ZoneInfo(config.KB_REFRESH_TIMEZONE)


def _now_local() -> datetime:
    return datetime.now(_tz())


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ── Release management ────────────────────────────────────────────────────────────
def active_root() -> Path:
    return config.resolve_active_kb_root()


def _copy_kb(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for sub in KB_SUBDIRS:
        if (src / sub).exists():
            shutil.copytree(src / sub, dst / sub, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.tmp", "metadata.pkl"))
    if (src / "kb_manifest.json").exists():
        shutil.copy2(src / "kb_manifest.json", dst / "kb_manifest.json")
    # Legacy flat layout kept crawl state in data/logs — carry it into the release.
    if src == config.DATA_DIR:
        (dst / "state").mkdir(parents=True, exist_ok=True)
        for f in config.LOGS_DIR.glob("*_crawl_state.json"):
            if not (dst / "state" / f.name).exists():
                shutil.copy2(f, dst / "state" / f.name)


def promote(release_dir: Path) -> None:
    """Atomically make ``release_dir`` the active release (single os.replace)."""
    config.RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.CURRENT_POINTER.with_suffix(".tmp")
    tmp.write_text(release_dir.name + "\n", encoding="utf-8")
    tmp.replace(config.CURRENT_POINTER)
    logger.info(f"Promoted release {release_dir.name}")


def prune_releases(keep: int = KEEP_RELEASES) -> None:
    if not config.RELEASES_DIR.exists():
        return
    current = config.CURRENT_POINTER.read_text().strip() if config.CURRENT_POINTER.exists() else ""
    rels = sorted([p for p in config.RELEASES_DIR.iterdir() if p.is_dir()], key=lambda p: p.name)
    ok = [p for p in rels if not p.name.startswith("failed-") and p.name != current]
    failed = [p for p in rels if p.name.startswith("failed-")]
    for p in ok[:-max(0, keep - 1)] if keep > 1 else ok:
        force_rmtree(p)
    for p in failed[:-2]:
        force_rmtree(p)


# ── Reporting ─────────────────────────────────────────────────────────────────────
def _summarise_source(res) -> Dict:
    c = res.counts or {}
    log = res.log or {}
    by_change: Dict[str, Counter] = {}
    for ch in res.changes:
        by_change.setdefault(ch["change"], Counter())[ch.get("content_type") or "unknown"] += 1
    return {
        "status": res.status,
        "error": res.error,
        "complete": res.complete,
        "pages_discovered": len(log),
        "pages_fetched": sum(1 for r in log.values() if r.get("status") is not None),
        "new": c.get("added", 0) + c.get("pdf_added", 0) + c.get("oped_added", 0),
        "modified": c.get("updated", 0) + c.get("pdf_updated", 0) + c.get("oped_updated", 0),
        "unchanged": c.get("unchanged", 0) + c.get("pdf_unchanged", 0) + c.get("oped_unchanged", 0),
        "failed": c.get("failed", 0),
        "redirected": c.get("redirected", 0),
        "gone_404": c.get("gone", 0),
        "removed": len(res.removed_ids),
        "removal_pending": c.get("removal_pending", 0),
        "by_change_and_type": {k: dict(v) for k, v in by_change.items()},
        "counts": c,
    }


def _write_crawl_log(source: str, res, run_id: str) -> None:
    out = config.REPORTS_DIR / "crawl"
    out.mkdir(parents=True, exist_ok=True)
    payload = {"source": source, "run_id": run_id, "status": res.status,
               "counts": res.counts, "changes": res.changes, "urls": res.log}
    (out / f"{source}_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                               encoding="utf-8")


def _load_status() -> Dict:
    p = config.DAILY_REPORTS_DIR / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"consecutive_failures": 0}


def _save_report(report: Dict) -> None:
    d = config.DAILY_REPORTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(report, ensure_ascii=False, indent=2)
    (d / "latest.json").write_text(txt, encoding="utf-8")
    day = report["started_at"][:10]
    (d / f"{day}.json").write_text(txt, encoding="utf-8")
    if report.get("dry_run"):
        return
    status = _load_status()
    status["last_attempt_date"] = report["started_at"][:10]      # local (IST) date, read by the gate
    if report.get("promoted"):
        status.update(last_success=report["completed_at"], last_success_run_id=report["run_id"])
    if report["status"] == "success":
        status["consecutive_failures"] = 0
    else:                                   # failed, or partial (a source failed but rest promoted)
        status["consecutive_failures"] = int(status.get("consecutive_failures", 0)) + 1
        status["last_failure"] = report["completed_at"]
        status["last_failure_reason"] = "; ".join(report.get("errors", []))[:500]
    status["last_run"] = report["completed_at"]
    status["last_run_status"] = report["status"]
    status["health"] = ("degraded" if status["consecutive_failures"] >= config.KB_REFRESH_DEGRADED_AFTER
                        else "ok")
    status["active_version"] = (report.get("index", {}).get("version") if report.get("promoted")
                                else status.get("active_version"))
    (d / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def next_scheduled_run(now: Optional[datetime] = None) -> datetime:
    now = now or _now_local()
    hh, mm = config.refresh_hour_minute()
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        from datetime import timedelta
        candidate += timedelta(days=1)
    return candidate


def refresh_status() -> Dict:
    """Administrator-facing summary (no Commit KB content)."""
    status = _load_status()
    latest = {}
    try:
        latest = json.loads((config.DAILY_REPORTS_DIR / "latest.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "health": status.get("health", "unknown"),
        "consecutive_failures": status.get("consecutive_failures", 0),
        "last_success": status.get("last_success"),
        "last_run": status.get("last_run"),
        "last_run_status": status.get("last_run_status"),
        "last_failure_reason": status.get("last_failure_reason"),
        "active_version": status.get("active_version"),
        "next_scheduled_run": _iso(next_scheduled_run()),
        "timezone": config.KB_REFRESH_TIMEZONE,
        "latest_totals": latest.get("totals"),
    }


# ── Smoke tests on a built root ───────────────────────────────────────────────────
def smoke_test() -> List[str]:
    """Load the active root as the server would and run real retrieval. Returns errors."""
    errors: List[str] = []
    try:
        from src import vector_store
        from src.retriever import retrieve
        st = vector_store.build_state()
        if st.bm25 is None:
            errors.append("BM25 index failed to build.")
        for q in SMOKE_QUERIES:
            hits = retrieve(q, top_k=3, state=st)
            if not hits:
                errors.append(f"Smoke query returned no results: {q!r}")
            elif not all(h.get("title") and (h.get("url") or h.get("source_file")) for h in hits):
                errors.append(f"Smoke query results missing citation metadata: {q!r}")
    except Exception as exc:
        errors.append(f"Smoke test crashed: {type(exc).__name__}: {exc}")
    return errors


# ── The refresh ───────────────────────────────────────────────────────────────────
def run_refresh(website: bool = True, commit_kb: bool = True, full: bool = False,
                dry_run: bool = False, promote_release: bool = True, rebuild_only: bool = False,
                progress_cb: Optional[Callable[[str], None]] = None,
                max_pages: Optional[int] = None, run_id: Optional[str] = None,
                extra_seeds: Optional[List[str]] = None) -> Dict:
    say = progress_cb or (lambda m: logger.info(m))
    started = _now_local()
    t0 = time.perf_counter()
    run_id = run_id or started.strftime("%Y%m%dT%H%M%S%z").replace("+", "p")
    prev_root = active_root()
    staging = config.RELEASES_DIR / run_id
    report: Dict = {"run_id": run_id, "started_at": _iso(started), "timezone": config.KB_REFRESH_TIMEZONE,
                    "mode": "full" if full else ("rebuild-only" if rebuild_only else "incremental"),
                    "dry_run": dry_run, "previous_root": str(prev_root), "errors": [], "warnings": [],
                    "promoted": False}
    original_root = config.KB_ROOT
    try:
        from scripts.validate_kb import summarize_counts, validate
        config.use_kb_root(prev_root)
        baseline = summarize_counts() if config.DOCUMENTS_FILE.exists() else None
        report["baseline"] = baseline

        say(f"Preparing staging release {staging.name} from {prev_root}")
        if staging.exists():
            force_rmtree(staging)
        _copy_kb(prev_root, staging)
        config.use_kb_root(staging)

        from scripts.crawl_engine import commit_kb_config, crawl_site, website_config
        from src.incremental_index import merge_documents, rebuild_index

        new_docs: List[Dict] = []
        removed: List[str] = []
        sources = []
        if not rebuild_only:
            if website:
                wc = website_config(max_pages=max_pages)
                wc.extra_seeds = list(extra_seeds or [])
                sources.append(("website", wc))
            if commit_kb:
                if config.COMMIT_KB_USERNAME and config.COMMIT_KB_PASSWORD:
                    sources.append(("commit_kb", commit_kb_config(max_pages=max_pages)))
                else:
                    report["commit_kb"] = {"status": "skipped", "error": "COMMIT_KB_USERNAME/PASSWORD not set"}
                    report["warnings"].append("Commit KB skipped: credentials not configured.")
        for name, site in sources:
            say(f"── Crawling {name} ({'full' if full else 'incremental'}) ──")
            try:
                res = crawl_site(site, incremental=not full, progress_cb=say)
            except Exception as exc:                      # isolate per-source failures
                logger.exception(f"{name} crawl crashed")
                report[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                report["errors"].append(f"{name} crawl crashed: {exc}")
                continue
            report[name] = _summarise_source(res)
            _write_crawl_log(name, res, run_id)
            if res.status == "partial":
                report["warnings"].append(
                    f"{name}: crawl stopped at the page cap (max_pages={site.max_pages}); "
                    "removal detection was skipped. Raise WEBSITE_MAX_PAGES.")
            if res.status == "aborted":
                report["errors"].append(f"{name}: {res.error}")
                continue                                   # keep that source's previous docs
            new_docs += res.docs
            removed += res.removed_ids

        attempted = [n for n, _ in sources]
        if attempted and all((report.get(n) or {}).get("status") in ("aborted", "failed") for n in attempted):
            raise RuntimeError("every crawled source failed — nothing to refresh; production KB kept")

        if dry_run:
            report["status"] = "dry_run"
            report["would_change"] = {"documents": len(new_docs), "removed": len(removed)}
            return report

        merge = merge_documents(new_docs, removed_ids=removed)
        report["merge"] = merge
        changed = merge["added"] + merge["updated"] + merge["removed"]
        from src.incremental_index import pipeline_version
        built_with = {}
        if config.KB_MANIFEST_FILE.exists():
            built_with = json.loads(config.KB_MANIFEST_FILE.read_text(encoding="utf-8"))
        pipeline_changed = built_with.get("pipeline_version") != pipeline_version()
        if pipeline_changed and not changed:
            say("Processing pipeline changed since the last build — re-indexing (cache-backed).")
        report["pipeline_changed"] = pipeline_changed
        need_index = changed or rebuild_only or pipeline_changed or not config.FAISS_INDEX.exists()
        if need_index:
            say("── Re-indexing (embedding only new/changed chunks) ──")
            idx = rebuild_index(progress_cb=say, use_cache=True, version=run_id)
            report["index"] = {"status": "success", **idx}
        else:
            say("No document changes — index carried over unchanged.")
            from src.incremental_index import write_kb_manifest
            man = json.loads(config.KB_MANIFEST_FILE.read_text(encoding="utf-8")) \
                if config.KB_MANIFEST_FILE.exists() else {}
            man.pop("version", None)
            man.pop("built_at", None)
            report["index"] = {**write_kb_manifest(man, version=run_id), "status": "unchanged",
                               "embedded": 0, "new_chunks": 0, "removed_chunks": 0}

        say("── Validating staging release ──")
        v = validate(baseline=baseline)
        smoke = smoke_test()
        v["smoke_errors"] = smoke
        report["validation"] = {"status": "passed" if (v["ok"] and not smoke) else "failed",
                                "errors": v["errors"] + smoke, "warnings": v["warnings"][:30],
                                "counts": v["counts"]}
        if report["validation"]["status"] != "passed":
            report["errors"] += report["validation"]["errors"]
            raise RuntimeError("validation failed — current production release kept")

        if promote_release:
            promote(staging)
            report["promoted"] = True
            report["active_root"] = str(staging)
            prune_releases()
        report["status"] = "success" if not report["errors"] else "partial"
        return report
    except Exception as exc:
        report["status"] = "failed"
        msg = f"{type(exc).__name__}: {exc}"
        if msg not in report["errors"]:
            report["errors"].append(msg)
        report["traceback"] = traceback.format_exc()[-2000:]
        logger.error(f"Refresh {run_id} failed: {msg}")
        if staging.exists() and not report["promoted"]:
            failed_dir = config.RELEASES_DIR / f"failed-{run_id}"
            try:
                force_rmtree(failed_dir)
                staging.rename(failed_dir)
            except OSError:
                force_rmtree(staging)
        return report
    finally:
        if dry_run and staging.exists():
            force_rmtree(staging)
        config.use_kb_root(config.resolve_active_kb_root() if report.get("promoted") else original_root)
        done = _now_local()
        report["completed_at"] = _iso(done)
        report["duration_seconds"] = round(time.perf_counter() - t0, 1)
        report["totals"] = _totals(report)
        report.setdefault("status", "failed")
        try:
            _save_report(report)
        except Exception as exc:
            logger.warning(f"could not write refresh report: {exc}")


def _totals(report: Dict) -> Dict:
    t = Counter()
    for name in ("website", "commit_kb"):
        s = report.get(name) or {}
        for k in ("pages_discovered", "pages_fetched", "new", "modified", "unchanged",
                  "failed", "redirected", "removed", "removal_pending"):
            t[k] += int(s.get(k) or 0)
    idx = report.get("index") or {}
    t["new_chunks"] = int(idx.get("new_chunks") or 0)
    t["modified_or_new_chunks_embedded"] = int(idx.get("embedded") or 0)
    t["embedding_count"] = int(idx.get("chunks") or 0)
    return dict(t)
