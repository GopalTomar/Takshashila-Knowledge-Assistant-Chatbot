#!/usr/bin/env python3
"""
smoke_test.py — Production smoke test for the deployed API (standard library only).

    python scripts/smoke_test.py --api https://<service>.onrender.com
    SMOKE_STAFF_TOKEN=<one of API_ACCESS_TOKENS> python scripts/smoke_test.py --api ...

Checks: /health, /ready (waits for a cold start), /rag/status KB version, a public
query (website-only citations, verified citation numbers), a staff query (internal
scope, only if SMOKE_STAFF_TOKEN is set), invalid auth → 401, Mattermost slash-token
and unsigned-callback rejection → 403, CORS for the GitHub Pages origin.
The staff token is read from the environment only and never printed.
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PAGES_ORIGIN = "https://gopaltomar.github.io"


def call(method, url, body=None, headers=None, timeout=120, form=False):
    data = None
    h = dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def js(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


class Runner:
    def __init__(self):
        self.failed = 0

    def check(self, name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failed += 1
        return ok


def citation_numbers_valid(answer: str, n_citations: int) -> bool:
    nums = {int(x) for x in re.findall(r"\[(?:Source\s*)?(\d+)\]", answer or "")}
    return all(1 <= x <= n_citations for x in nums)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True, help="https://<service>.onrender.com")
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for /ready (cold start)")
    ap.add_argument("--origin", default=PAGES_ORIGIN)
    a = ap.parse_args(argv)
    api = a.api.rstrip("/")
    staff = os.getenv("SMOKE_STAFF_TOKEN", "")
    t = Runner()

    s, _, _ = call("GET", f"{api}/health", timeout=180)          # first call may wake the service
    t.check("GET /health", s == 200, f"HTTP {s}")

    deadline, s = time.time() + a.wait, 0
    while time.time() < deadline:
        s, _, raw = call("GET", f"{api}/ready", timeout=60)
        if s == 200:
            break
        time.sleep(10)
    t.check("GET /ready", s == 200, f"HTTP {s}")

    s, _, raw = call("GET", f"{api}/rag/status")
    st = js(raw)
    kb = st.get("kb") or {}
    t.check("KB version reported", s == 200 and kb.get("loaded") and bool(kb.get("version")),
            f"version={kb.get('version')} vectors={kb.get('vectors')}")

    q = {"query": "What has Takshashila published about geospatial technology?"}
    s, _, raw = call("POST", f"{api}/api/query", q)
    r = js(raw)
    cites = r.get("citations") or []
    t.check("public query answered", s == 200 and bool(r.get("answer")), f"HTTP {s}")
    t.check("public query cites only website sources",
            bool(cites) and all(c.get("source") == "website" for c in cites),
            f"{len(cites)} citations: {sorted({c.get('source') for c in cites})}")
    t.check("citation numbers map to returned citations",
            citation_numbers_valid(r.get("answer", ""), len(cites)))
    t.check("citations carry https URLs", all(str(c.get("url", "")).startswith("https://") for c in cites))
    meta = r.get("metadata") or {}
    t.check("query reports scope=public + KB version",
            meta.get("scope") == "public" and bool(meta.get("kb_version")),
            f"scope={meta.get('scope')} kb_version={meta.get('kb_version')}")

    iq = {"query": "What internal decisions has Takshashila recorded?", "mode": "search"}
    s, _, raw = call("POST", f"{api}/api/query", iq)
    leaked = [c for c in (js(raw).get("citations") or []) if c.get("source") != "website"]
    t.check("anonymous user gets no internal sources", s == 200 and not leaked, f"{len(leaked)} internal")

    s, _, _ = call("POST", f"{api}/api/query", iq, headers={"Authorization": "Bearer invalid-token"})
    t.check("invalid staff token rejected", s == 401, f"HTTP {s}")

    if staff:
        s, _, raw = call("POST", f"{api}/api/query", iq, headers={"Authorization": f"Bearer {staff}"})
        m = js(raw).get("metadata") or {}
        t.check("staff query uses internal scope", s == 200 and m.get("scope") == "internal", f"HTTP {s}")
    else:
        print("[SKIP] staff query — set SMOKE_STAFF_TOKEN to test")

    s, _, _ = call("POST", f"{api}/mattermost/ask", {"token": "invalid", "text": "hello"}, form=True)
    t.check("Mattermost: wrong slash token rejected", s == 403, f"HTTP {s}")
    s, _, _ = call("POST", f"{api}/mattermost/action",
                   {"context": {"action": "export_pdf", "question": "q"}, "channel_id": "x"})
    t.check("Mattermost: unsigned callback rejected", s == 403, f"HTTP {s}")

    pre = {"Origin": a.origin, "Access-Control-Request-Method": "POST",
           "Access-Control-Request-Headers": "content-type"}
    s, h, _ = call("OPTIONS", f"{api}/api/query", headers=pre)
    allow = {k.lower(): v for k, v in h.items()}.get("access-control-allow-origin")
    t.check(f"CORS allows {a.origin}", s == 200 and allow == a.origin, f"HTTP {s} allow={allow}")
    s, h, _ = call("OPTIONS", f"{api}/api/query", headers={**pre, "Origin": "https://evil.example"})
    allow = {k.lower(): v for k, v in h.items()}.get("access-control-allow-origin")
    t.check("CORS refuses other origins", allow is None, f"HTTP {s}")

    print(f"\n{'ALL CHECKS PASSED' if not t.failed else f'{t.failed} CHECK(S) FAILED'}")
    return 0 if not t.failed else 1


if __name__ == "__main__":
    sys.exit(main())
