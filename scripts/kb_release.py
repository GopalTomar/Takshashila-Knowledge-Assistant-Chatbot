#!/usr/bin/env python3
"""
kb_release.py — Move KB releases between the refresh job and the API.

    python scripts/kb_release.py restore --from-dir downloaded/   # before a refresh
    python scripts/kb_release.py pack --out-dir dist-kb/          # after a successful refresh
    python scripts/kb_release.py status --out kb-status.json      # always (public, no content)

``restore`` unpacks the last published bundle (kb-manifest.json + kb-<version>.tkkb)
into data/releases/<version> and points data/releases/CURRENT at it, so the
refresh is incremental against the last published KB (crawl state, embedding
cache, documents). With no previous bundle the refresh bootstraps from scratch.

``pack`` copies the refresh reports into the active release and writes the
encrypted bundle + manifest. ``status`` writes the small public status file the
scheduler gate reads (last attempt date, health, counts — no KB content).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, kb_bundle                          # noqa: E402
from src.utils import force_rmtree                          # noqa: E402

PUBLIC_STATUS_KEYS = ("health", "consecutive_failures", "last_attempt_date", "last_run",
                      "last_run_status", "last_success", "last_success_run_id", "active_version",
                      "last_failure", "last_failure_reason")


def restore(from_dir: Path) -> int:
    status = from_dir / "kb-status.json"
    if status.exists():
        config.DAILY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(status, config.DAILY_REPORTS_DIR / "status.json")
    man_path = from_dir / "kb-manifest.json"
    if not man_path.exists():
        print("No previous KB release — the refresh will bootstrap a full build.")
        return 0
    man = json.loads(man_path.read_text(encoding="utf-8"))
    bundle = from_dir / man["asset"]
    dest = config.RELEASES_DIR / str(man["version"])
    kb_bundle.unpack(bundle, dest, config.KB_BUNDLE_KEY, expected_sha256=man.get("sha256", ""))
    config.RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    config.CURRENT_POINTER.write_text(dest.name + "\n", encoding="utf-8")
    print(f"Restored KB release {dest.name}")
    return 0


def pack(out_dir: Path) -> int:
    root = config.resolve_active_kb_root()
    if not (root / "index" / "faiss.index").exists():
        print("No active KB release to pack.", file=sys.stderr)
        return 1
    rep = root / "reports"
    force_rmtree(rep)                     # plain rmtree fails on OneDrive read-only dirs
    for sub in ("daily_refresh", "crawl"):
        if (config.REPORTS_DIR / sub).exists():
            shutil.copytree(config.REPORTS_DIR / sub, rep / sub, dirs_exist_ok=True)
    for f in config.REPORTS_DIR.glob("*.json"):
        rep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, rep / f.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    version = json.loads((root / "kb_manifest.json").read_text(encoding="utf-8"))["version"]
    man = kb_bundle.pack(root, out_dir / f"kb-{version}.tkkb", config.KB_BUNDLE_KEY)
    (out_dir / "kb-manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    print(json.dumps(man, indent=2))
    return 0


def status(out: Path) -> int:
    try:
        st = json.loads((config.DAILY_REPORTS_DIR / "status.json").read_text(encoding="utf-8"))
    except Exception:
        st = {}
    try:
        latest = json.loads((config.DAILY_REPORTS_DIR / "latest.json").read_text(encoding="utf-8"))
    except Exception:
        latest = {}
    public = {k: st.get(k) for k in PUBLIC_STATUS_KEYS}
    public["totals"] = latest.get("totals")
    public["timezone"] = config.KB_REFRESH_TIMEZONE
    out.write_text(json.dumps(public, indent=2), encoding="utf-8")
    print(json.dumps(public, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--from-dir", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--out-dir", required=True)
    s = sub.add_parser("status")
    s.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "restore":
        return restore(Path(args.from_dir))
    if args.cmd == "pack":
        return pack(Path(args.out_dir))
    return status(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
