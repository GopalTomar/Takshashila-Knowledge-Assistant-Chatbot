"""Crawl engine behaviour against an in-memory fake website (no network)."""

from __future__ import annotations

import pytest
import requests

import scripts.crawl_engine as ce
from src import config
from src.crawl_state import CrawlState

W = "https://takshashila.org.in"


def page(title, body, author=None, date="March 9, 2026", links=()):
    a = f'<tr><td>AUTHOR</td><td><a href="/content/team/x.html">{author}</a></td></tr>' if author else ""
    ls = "".join(f'<a href="{l}">link</a>' for l in links)
    return (f'<html><body><header id="title-block-header"><h1 class="title">{title}</h1>'
            f'<table>{a}<tr><td>DATE</td><td>{date}</td></tr></table></header>'
            f'<main id="quarto-document-content"><p>{body}</p><p>{ls}</p></main></body></html>')


def make_pdf_bytes(text="Annex text about satellites and policy. " * 20):
    import pymupdf as fitz
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 72), text[:90])
    p.insert_text((50, 100), text[90:180])
    b = doc.tobytes()
    doc.close()
    return b


class FakeResp:
    def __init__(self, url, status=200, body=b"", ctype="text/html", headers=None, history=()):
        self.url, self.status_code, self.content = url, status, body
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self.history = list(history)
        self.encoding = "utf-8"

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class FakeSite:
    """routes: url -> dict(status, body, ctype, redirect, etag, raise, fail_times)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.auth = None
        self.headers = {}

    def mount(self, *a):
        pass

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, dict(headers or {})))
        r = self.routes.get(url)
        if r is None:
            return FakeResp(url, 404, b"not found")
        if r.get("fail_times", 0) > 0:
            r["fail_times"] -= 1
            raise requests.Timeout("simulated timeout")
        if r.get("raise"):
            raise r["raise"]
        if r.get("redirect"):
            final = self.get(r["redirect"], headers, timeout)
            final.history = [FakeResp(url, 301)]
            return final
        if r.get("etag") and headers and headers.get("If-None-Match") == r["etag"]:
            return FakeResp(url, 304)
        body = r.get("body", "")
        body = body.encode() if isinstance(body, str) else body
        return FakeResp(url, r.get("status", 200), body, r.get("ctype", "text/html"),
                        {"ETag": r["etag"]} if r.get("etag") else {})


def site_config(**kw):
    s = ce.SiteConfig(base_url=f"{W}/", domain="takshashila.org.in", source="website",
                      source_name="Takshashila Website", sitemap_urls=[f"{W}/sitemap.xml"],
                      oped_listing_paths=["/pages/news/"], min_text_len=40, max_pages=100,
                      max_depth=5, include_pdfs=True, respect_robots=True, max_workers=2)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def sitemap(*urls):
    locs = "".join(f"<url><loc>{u}</loc><lastmod>2026-09-01T00:00:00Z</lastmod></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


@pytest.fixture(autouse=True)
def _fast(monkeypatch, kb_root):
    monkeypatch.setattr(ce.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "SCRAPE_MAX_RPS", 0)
    monkeypatch.setattr(config, "SCRAPE_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "KB_REMOVAL_CONFIRMATIONS", 2)


def base_routes():
    pub = f"{W}/content/publications/geo.html"
    blog = f"{W}/content/blogs/chips.html"
    return {
        f"{W}/": {"body": page("Takshashila Institution", "Home page " * 20, links=[pub, "/pages/news/"])},
        f"{W}/robots.txt": {"body": "User-agent: *\nDisallow: /private/\n", "ctype": "text/plain"},
        f"{W}/sitemap.xml": {"body": sitemap(pub, blog, f"{W}/private/secret.html"), "ctype": "application/xml"},
        pub: {"body": page("Geospatial Report", "Satellite imagery helps India monitor borders. " * 5,
                           author="Y Nithiyanandam", links=["assets/geo.pdf", "/content/blogs/chips.html#top"]),
              "etag": '"v1"'},
        blog: {"body": page("Chips Blog", "Semiconductor policy needs patience and skills. " * 5,
                            author="Pranay Kotasthane")},
        f"{W}/content/publications/assets/geo.pdf": {"body": make_pdf_bytes(), "ctype": "application/pdf"},
        f"{W}/pages/news/": {"body": """<html><body><main id="quarto-document-content">
            <div class="quarto-post"><h3 class="listing-title"><a href="https://news.example/a">Op-ed A</a></h3>
            <div class="listing-date">Sep 1, 2026</div><div class="listing-authors">Pranay Kotasthane</div>
            <div class="listing-source">The Hindu</div></div></main></body></html>"""},
        f"{W}/private/secret.html": {"body": page("Secret", "should not be fetched " * 10)},
    }


def run(routes, incremental=True, **kw):
    state = CrawlState("website")
    eng = ce.CrawlEngine(site_config(**kw), state, incremental=incremental, session=FakeSite(routes))
    return eng.crawl(), eng, state


def test_full_crawl_discovers_everything_and_respects_robots():
    res, eng, _ = run(base_routes())
    types = {d["content_type"] for d in res.docs}
    assert {"publication", "blog", "pdf", "op-ed"} <= types
    urls = {d["url"] for d in res.docs}
    assert f"{W}/private/secret.html" not in urls                    # robots.txt honoured
    assert eng.log[f"{W}/private/secret.html"]["outcome"] == "robots_blocked"
    pub = next(d for d in res.docs if d["content_type"] == "publication")
    assert pub["authors"] == ["Y Nithiyanandam"] and pub["date"] == "2026-03-09"
    assert pub["modified_date"] == "2026-09-01"                        # sitemap lastmod
    pdf = next(d for d in res.docs if d["content_type"] == "pdf")
    assert pdf["parent_url"] == pub["url"] and pdf["title"].endswith("[PDF]")
    oped = next(d for d in res.docs if d["content_type"] == "op-ed")
    assert oped["url"] == "https://news.example/a" and oped["authors"] == ["Pranay Kotasthane"]
    assert res.complete and res.status == "success"
    # fragment link resolved to the page (not refused)
    assert f"{W}/content/blogs/chips.html" in eng.log


def test_extractor_change_forces_one_full_reextraction(monkeypatch):
    """An extraction-code fix must reach pages that still answer 304."""
    used = []

    class Spy:
        def __init__(self, site, state, incremental=True, **kw):
            used.append(incremental)
            self.log = {}

        def crawl(self):
            return ce.CrawlResult(status="success")

    monkeypatch.setattr(ce, "CrawlEngine", Spy)
    st = CrawlState("website")
    st.urls = {f"{W}/a.html": {"content_hash": "x"}}
    st.extractor_version = "old"
    st.save()
    ce.crawl_site(site_config())                     # fingerprint differs → full once
    assert CrawlState("website").extractor_version == ce.extractor_version()
    ce.crawl_site(site_config())                     # now incremental again
    assert used == [False, True]


def test_incremental_unchanged_then_changed():
    routes = base_routes()
    run(routes)
    res2, eng2, _ = run(routes)
    assert res2.docs == []                                             # nothing reprocessed
    assert eng2.log[f"{W}/content/publications/geo.html"]["outcome"] == "not_modified"   # 304 via ETag
    routes[f"{W}/content/blogs/chips.html"]["body"] = page("Chips Blog", "Updated analysis of fabs. " * 8,
                                                           author="Pranay Kotasthane")
    res3, _, _ = run(routes)
    assert [d["url"] for d in res3.docs] == [f"{W}/content/blogs/chips.html"]
    assert any(c["change"] == "modified" for c in res3.changes)


def test_removal_needs_confirmations_and_failures_keep_docs():
    routes = base_routes()
    run(routes)
    blog = f"{W}/content/blogs/chips.html"
    del routes[blog]                                                   # now 404
    routes[f"{W}/sitemap.xml"]["body"] = sitemap(f"{W}/content/publications/geo.html")
    res1, _, st = run(routes)
    assert res1.removed_ids == []                                      # first miss: pending only
    assert st.get(blog)["missing_count"] == 1
    res2, _, _ = run(routes)
    assert "website_" + __import__("src.utils", fromlist=["url_hash"]).url_hash(blog) in res2.removed_ids


def test_temporary_failure_is_retried_and_does_not_remove():
    routes = base_routes()
    run(routes)
    pub = f"{W}/content/publications/geo.html"
    routes[pub] = {"status": 503, "body": "down"}
    res, eng, st = run(routes)
    assert eng.log[pub]["outcome"] == "failed" and eng.log[pub]["retries"] == 2
    assert res.removed_ids == [] and st.get(pub).get("unavailable_since")


def test_timeout_then_success_counts_retries():
    routes = base_routes()
    routes[f"{W}/content/blogs/chips.html"]["fail_times"] = 1
    res, eng, _ = run(routes)
    rec = eng.log[f"{W}/content/blogs/chips.html"]
    assert rec["status"] == 200 and rec["retries"] == 1


def test_redirect_moves_document():
    routes = base_routes()
    run(routes)
    old, new = f"{W}/content/blogs/chips.html", f"{W}/content/blogs/chips-v2.html"
    routes[new] = routes[old]
    routes[old] = {"redirect": new}
    res, eng, _ = run(routes)
    assert eng.log[old]["final_url"] == new
    assert any(d["url"] == new for d in res.docs)
    assert any(c["change"] == "redirected" for c in res.changes)


def test_auth_failure_aborts_without_changes():
    routes = base_routes()
    routes[f"{W}/"] = {"status": 401, "body": "unauthorised"}
    res = None
    state = CrawlState("website")
    eng = ce.CrawlEngine(site_config(), state, session=FakeSite(routes))
    with pytest.raises(ce.CrawlAborted):
        eng.crawl()
    assert res is None and state.urls == {}


def test_page_cap_marks_partial_and_skips_removals():
    res, _, _ = run(base_routes(), max_pages=2)
    assert res.status == "partial" and not res.complete and res.removed_ids == []


def test_pdfs_rechecked_when_parent_page_is_not_modified():
    routes = base_routes()
    run(routes)
    pdf = f"{W}/content/publications/assets/geo.pdf"
    routes[pdf]["body"] = make_pdf_bytes("A revised annex about satellite constellations and policy. " * 10)
    res, eng, _ = run(routes)
    assert eng.log[f"{W}/content/publications/geo.html"]["outcome"] == "not_modified"
    changed = [d for d in res.docs if d["url"] == pdf]
    assert changed and changed[0]["title"] == "Geospatial Report [PDF]"      # parent meta from state
    assert changed[0]["authors"] == ["Y Nithiyanandam"]


def test_unlinked_but_live_pdf_is_never_removed():
    routes = base_routes()
    run(routes)
    pub = f"{W}/content/publications/geo.html"
    routes[pub]["body"] = page("Geospatial Report", "Satellite imagery helps India monitor borders. " * 5,
                               author="Y Nithiyanandam")          # PDF link dropped, PDF still online
    routes[pub].pop("etag")
    for _ in range(3):
        res, eng, st = run(routes)
        assert res.removed_ids == []
    assert st.document_id(f"{W}/content/publications/assets/geo.pdf")


def test_unlinked_and_404_pdf_is_removed_after_confirmations():
    routes = base_routes()
    run(routes)
    pdf = f"{W}/content/publications/assets/geo.pdf"
    pub = f"{W}/content/publications/geo.html"
    routes[pub]["body"] = page("Geospatial Report", "Satellite imagery helps India monitor borders. " * 5)
    routes[pub].pop("etag")
    del routes[pdf]
    r1, _, _ = run(routes)
    r2, _, _ = run(routes)
    assert r1.removed_ids == [] and any("_pdf_" in d for d in r2.removed_ids)
