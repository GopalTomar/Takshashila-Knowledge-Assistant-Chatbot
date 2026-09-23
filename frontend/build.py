#!/usr/bin/env python3
"""
Build the static GitHub Pages site into frontend/dist/.

    API_BASE_URL=https://<your-api>.up.railway.app python frontend/build.py

Only the PUBLIC API base URL is injected (into config.js). Nothing secret is ever
written to the site. The build fails if the URL is missing (unless
--allow-empty-api) or is not https (http is allowed only for localhost dev).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["index.html", "styles.css", "app.js"]
_SECRET_HINTS = re.compile(r"(gsk_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|COMMIT_KB_PASSWORD|MATTERMOST_BOT_TOKEN)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "dist"))
    ap.add_argument("--allow-empty-api", action="store_true")
    args = ap.parse_args(argv)

    api = os.getenv("API_BASE_URL", "").strip().rstrip("/")
    if not api and not args.allow_empty_api:
        print("ERROR: API_BASE_URL is not set (e.g. https://takshashila-rag.up.railway.app)", file=sys.stderr)
        return 2
    if api and not (api.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", api)):
        print(f"ERROR: API_BASE_URL must be https:// (got {api!r})", file=sys.stderr)
        return 2

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    (out / "assets").mkdir(parents=True)
    for f in FILES:
        shutil.copy2(HERE / f, out / f)
    for a in (HERE / "assets").iterdir():
        shutil.copy2(a, out / "assets" / a.name)
    cfg = {"apiBaseUrl": api}
    (out / "config.js").write_text(f"window.TK_CONFIG = {json.dumps(cfg)};\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    # Defence in depth: refuse to publish anything that looks like a secret.
    for p in out.rglob("*"):
        if p.is_file() and p.suffix in (".html", ".js", ".css", ".json"):
            if _SECRET_HINTS.search(p.read_text(encoding="utf-8", errors="ignore")):
                print(f"ERROR: possible secret found in {p}", file=sys.stderr)
                return 3
    print(f"Built {out} (API: {api or 'not configured'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
