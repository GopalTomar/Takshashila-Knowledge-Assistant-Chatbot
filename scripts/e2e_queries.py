#!/usr/bin/env python3
"""
e2e_queries.py — End-to-end checks against the REAL active KB and LLM.

    python scripts/e2e_queries.py [--out data/reports/e2e_results.json]

For each question it runs the production pipeline (internal scope, like
Mattermost) and verifies automatically:
  * insufficient-evidence questions are refused with no citations;
  * every other answer has ≥1 citation, every [Source N] marker maps to a shown
    citation, every shown citation is referenced, URLs are http(s) and belong to
    a document in the active KB, titles match the KB record;
  * expectation hooks: required source, author, URL substring.
The output contains answers — it may include internal Commit KB text, so it is
written under data/reports/ (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

CASES = [
    {"id": 1, "q": "What is Takshashila Institution?", "expect": {}},
    {"id": 2, "q": "What are Takshashila's research areas?", "expect": {}},
    {"id": 3, "q": "What has Takshashila published about geospatial technology?", "expect": {}},
    {"id": 4, "q": "Find publications by Pranay Kotasthane", "expect": {"author": "Pranay Kotasthane"}},
    {"id": 5, "q": "What blogs and op-eds has Anupam Manur written?", "expect": {"author": "Anupam Manur"}},
    {"id": 6, "q": "Tell me about the Geospatial Research programme at Takshashila",
     "expect": {"url_contains": "geospatial"}},
    {"id": 7, "q": "European Rearmament: Precursors, Policies, and Possibilities for India",
     "expect": {"url_contains": "EU-Rearm"}},
    {"id": 8, "q": "What internal decisions and playbook entries has Takshashila recorded?", "expect": {"source": "commit_kb"}},
    {"id": 9, "q": "What courses does the Takshashila Policy School offer?", "expect": {"source": "website"}},
    {"id": 10, "q": "What does Takshashila's internal playbook say about AI use, and what has it published on AI governance?",
     "expect": {"sources_all": ["commit_kb", "website"]}},
    {"id": 11, "q": "What was the population of the Takshashila lunar colony in 2051?", "expect": {"refuse": True}},
]
_MARK = re.compile(r"\[Source (\d+)\]")


def check(case, res, kb_urls, kb_titles):
    problems = []
    cites = res.get("citations") or []
    exp = case["expect"]
    refused = res.get("confidence") == "none" and not cites
    if exp.get("refuse"):
        if not refused:
            problems.append("expected an insufficient-evidence refusal")
        return problems
    if refused:
        return ["refused (insufficient evidence)"]
    marks = {int(m) for m in _MARK.findall(res["answer"])}
    nums = {c["n"] for c in cites}
    if marks - nums:
        problems.append(f"markers without citation: {sorted(marks - nums)}")
    if res.get("grounding", {}).get("attribution") == "cited" and nums - marks:
        problems.append(f"citations never referenced: {sorted(nums - marks)}")
    for c in cites:
        if c["url"] and not c["url"].startswith(("http://", "https://")):
            problems.append(f"bad URL {c['url']}")
        if c["url"] and c["url"] not in kb_urls:
            problems.append(f"URL not in KB: {c['url']}")
        if c["url"] in kb_titles and kb_titles[c["url"]] != c["title"]:
            problems.append(f"title mismatch for {c['url']}")
    srcs = {c["source"] for c in cites}
    if exp.get("source") and exp["source"] not in srcs:
        problems.append(f"expected a {exp['source']} citation, got {sorted(srcs)}")
    for s in exp.get("sources_all", []):
        if s not in srcs:
            problems.append(f"expected a {s} citation too")
    if exp.get("author") and not any(exp["author"] in (c.get("authors") or []) or
                                     exp["author"].lower() in c["title"].lower() for c in cites):
        problems.append(f"no citation authored by {exp['author']}")
    if exp.get("url_contains") and not any(exp["url_contains"].lower() in c["url"].lower() for c in cites):
        problems.append(f"no citation URL containing {exp['url_contains']!r}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.REPORTS_DIR / "e2e_results.json"))
    args = ap.parse_args(argv)
    from src.incremental_index import load_all_documents
    from src.rag_pipeline import answer
    docs = load_all_documents()
    kb_urls = {d["url"] for d in docs if d.get("url")}
    kb_titles = {d["url"]: d["title"] for d in docs if d.get("url")}
    out, failures = [], 0
    for case in CASES:
        t = time.perf_counter()
        try:
            res = answer(case["q"], allowed_sources=config.INTERNAL_SOURCES)
        except Exception as exc:
            res = {"answer": f"ERROR {type(exc).__name__}: {exc}", "citations": [], "confidence": "none"}
        dt = round(time.perf_counter() - t, 2)
        problems = (["upstream error (not a refusal): " + res["answer"][:120]]
                    if res.get("answer", "").startswith("ERROR ") else check(case, res, kb_urls, kb_titles))
        failures += bool(problems)
        rec = {"id": case["id"], "question": case["q"], "ok": not problems, "problems": problems,
               "latency_seconds": dt, "confidence": res.get("confidence"),
               "grounding": res.get("grounding"), "answer": res.get("answer"),
               "citations": [{k: c.get(k) for k in ("n", "title", "url", "source", "content_type",
                                                    "authors", "date")} for c in res.get("citations") or []]}
        out.append(rec)
        print(f"[{'PASS' if not problems else 'FAIL'}] Q{case['id']} ({dt}s, {rec['confidence']}, "
              f"{len(rec['citations'])} cites) {case['q'][:70]}")
        for p in problems:
            print(f"      - {p}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed — details in {args.out}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
