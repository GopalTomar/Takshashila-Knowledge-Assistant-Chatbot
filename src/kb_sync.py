"""
kb_sync.py — Keep a running API on the latest published KB release.

The API (Render) never crawls. It polls the public release manifest written by
the scheduled refresh job; when a new version appears it:

  1. downloads the encrypted bundle to data/releases/<version>.download,
  2. verifies size + sha256 against the manifest,
  3. decrypts + extracts into data/releases/<version>.partial (tar-slip safe),
  4. builds a complete serving state from it (FAISS + metadata + BM25) and runs
     a retrieval smoke query — nothing is swapped if any step fails,
  5. renames .partial → <version>, rewrites data/releases/CURRENT atomically,
     re-points config and swaps the in-memory state in one assignment.

In-flight requests finish on the old state; the next request sees the new one.
A failed sync leaves the previous KB serving and is reported by /api/health.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

from src import config
from src.utils import force_rmtree, get_logger

logger = get_logger("kb_sync", config.SCRAPE_LOG)

_status: Dict = {"enabled": False, "last_check": None, "last_error": None,
                 "last_activated": None, "remote_version": None}
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def status() -> Dict:
    return dict(_status)


def enabled() -> bool:
    return bool(config.KB_BUNDLE_MANIFEST_URL and config.KB_BUNDLE_KEY)


def _headers() -> Dict[str, str]:
    h = {"User-Agent": "takshashila-kb-sync", "Accept": "application/octet-stream"}
    if config.KB_BUNDLE_TOKEN:
        h["Authorization"] = f"Bearer {config.KB_BUNDLE_TOKEN}"
    return h


def _asset_url(manifest: Dict) -> str:
    if manifest.get("asset_url"):
        return manifest["asset_url"]
    base = config.KB_BUNDLE_MANIFEST_URL.rsplit("/", 1)[0]
    return f"{base}/{manifest['asset']}"


def fetch_manifest() -> Dict:
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        r = c.get(config.KB_BUNDLE_MANIFEST_URL, headers={**_headers(), "Accept": "application/json"})
        r.raise_for_status()
        return r.json()


def current_version() -> str:
    try:
        from src import vector_store
        if vector_store.is_loaded():
            return vector_store.get_state().version
    except Exception:
        pass
    try:
        return json.loads(config.KB_MANIFEST_FILE.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        return ""


def _checked_state(release_dir: Path):
    from src import vector_store
    from src.retriever import retrieve
    state = vector_store.build_state(root=release_dir)
    if state.ntotal == 0:
        raise ValueError("release has an empty index")
    if not retrieve("Takshashila Institution", top_k=2, state=state):
        raise ValueError("release smoke query returned no results")
    return state


def _precheck_on_disk(release_dir: Path) -> None:
    """Cheap structural checks before any in-memory state is released."""
    for rel in ("index/faiss.index", "index/metadata.json", "kb_manifest.json"):
        f = release_dir / rel
        if not f.exists() or f.stat().st_size == 0:
            raise ValueError(f"release is missing {rel}")
    json.loads((release_dir / "kb_manifest.json").read_text(encoding="utf-8"))


def _build_low_memory(release_dir: Path):
    """
    KB_LOW_MEMORY: never hold two KB states at once. The new release is already
    downloaded, verified and unpacked on disk; release the old state, load the new
    one, and if that fails load the previous release back from disk.
    """
    import gc
    from src import vector_store
    _precheck_on_disk(release_dir)
    previous = Path(config.KB_ROOT) if vector_store.is_loaded() else None
    vector_store.suspend()
    gc.collect()
    vector_store.release_free_memory()
    try:
        return _checked_state(release_dir)
    except Exception:
        if previous is not None and (previous / "index" / "faiss.index").exists():
            logger.error(f"New release {release_dir.name} failed to load — restoring {previous.name}.")
            try:
                vector_store.set_state(vector_store.build_state(root=previous))
            except Exception as exc:                       # pragma: no cover — disk damaged
                logger.error(f"Could not restore previous release: {exc}")
                vector_store.resume()
        else:
            vector_store.resume()
        raise


def activate_release(release_dir: Path) -> None:
    """Build a serving state from ``release_dir`` and swap it in (validated first)."""
    from src import vector_store
    state = _build_low_memory(release_dir) if config.KB_LOW_MEMORY else _checked_state(release_dir)
    config.RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.CURRENT_POINTER.with_suffix(".tmp")
    tmp.write_text(release_dir.name + "\n", encoding="utf-8")
    tmp.replace(config.CURRENT_POINTER)
    config.use_kb_root(release_dir)
    vector_store.set_state(state)
    _status["last_activated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    logger.info(f"Activated KB release {release_dir.name} ({state.ntotal} vectors)")


def sync_once(force: bool = False) -> bool:
    """Check the manifest and activate a newer release. Returns True if activated."""
    if not enabled():
        return False
    with _lock:
        _status["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            man = fetch_manifest()
            version = str(man["version"])
            _status["remote_version"] = version
            if not force and version == current_version():
                _status["last_error"] = None
                return False
            from src import kb_bundle
            config.RELEASES_DIR.mkdir(parents=True, exist_ok=True)
            final_dir = config.RELEASES_DIR / version
            if not (final_dir / "index" / "faiss.index").exists():
                dl = config.RELEASES_DIR / f"{version}.download"
                partial = config.RELEASES_DIR / f"{version}.partial"
                shutil.rmtree(partial, ignore_errors=True)
                with httpx.Client(timeout=httpx.Timeout(60, read=300), follow_redirects=True) as c:
                    with c.stream("GET", _asset_url(man), headers=_headers()) as r:
                        r.raise_for_status()
                        with open(dl, "wb") as f:
                            for chunk in r.iter_bytes(1 << 20):
                                f.write(chunk)
                if man.get("size") and dl.stat().st_size != int(man["size"]):
                    raise ValueError("downloaded bundle size mismatch")
                kb_bundle.unpack(dl, partial, config.KB_BUNDLE_KEY, expected_sha256=man.get("sha256", ""))
                dl.unlink(missing_ok=True)
                shutil.rmtree(final_dir, ignore_errors=True)
                partial.rename(final_dir)
            activate_release(final_dir)
            _prune(keep=2)
            _status["last_error"] = None
            return True
        except Exception as exc:
            for leftover in list(config.RELEASES_DIR.glob("*.download")) + \
                    list(config.RELEASES_DIR.glob("*.partial")):
                if leftover.is_dir():
                    force_rmtree(leftover)
                else:
                    leftover.unlink(missing_ok=True)
            _status["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
            logger.error(f"KB sync failed (previous KB still serving): {_status['last_error']}")
            return False


def _prune(keep: int = 2) -> None:
    current = config.CURRENT_POINTER.read_text().strip() if config.CURRENT_POINTER.exists() else ""
    dirs = sorted(p for p in config.RELEASES_DIR.iterdir()
                  if p.is_dir() and not p.name.endswith((".partial",)) and p.name != current)
    for p in dirs[:-max(0, keep - 1)] if keep > 1 else dirs:
        force_rmtree(p)


def _loop() -> None:
    while True:
        time.sleep(max(60, config.KB_SYNC_INTERVAL_MINUTES * 60))
        sync_once()


def start_background_sync() -> None:
    """Start the polling thread (idempotent)."""
    global _thread
    _status["enabled"] = enabled()
    if not enabled() or (_thread and _thread.is_alive()):
        return
    _thread = threading.Thread(target=_loop, name="kb-sync", daemon=True)
    _thread.start()
