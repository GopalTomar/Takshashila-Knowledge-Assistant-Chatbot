"""Quarto-aware extraction on fixtures that mirror takshashila.org.in markup."""

import base64
from urllib.parse import quote

from src.site_extract import extract_listing_entries, extract_page

PUB = """<html lang="en"><head><title>European Rearmament – Takshashila Institution</title>
<meta name="dcterms.date" content="2026-03-09">
<meta name="description" content="This paper finds that Europe is rearming."></head>
<body><header id="title-block-header"><h1 class="title">European Rearmament</h1>
<div class="gcpp-hero__lede">Precursors, Policies, and Possibilities for India</div>
<table><tr><td><strong>AUTHOR</strong></td><td><a href="../../content/team/anushka-saxena.html">Anushka Saxena</a>,
<a href="../../content/team/b-c.html">Bee Cee</a></td></tr>
<tr><td><strong>DATE</strong></td><td>March 9, 2026</td></tr>
<tr><td><strong>DOCUMENT</strong></td><td>Takshashila Discussion Document 2025-06</td></tr>
<tr><td><strong>VERSION</strong></td><td>Version 1.0, March 2026</td></tr>
<tr><td><strong>CATEGORIES</strong></td><td><a class="tk-tag" href="#">Defence</a><a class="tk-tag" href="#">Indian Foreign Policy</a></td></tr>
</table><script>var x = 1;</script></header>
<main id="quarto-document-content"><p>Download Document</p>
<h1>Executive Summary</h1><p>Europe is shifting to a war economy.</p>
<ul><li>Point one about ReArm.</li><li>Point two about SAFE loans.</li></ul>
<p><a href="assets/eu-rearm.pdf">PDF</a> <a href="https://www.nato.int/x">NATO</a></p>
<script>document.write("noise")</script></main><footer class="tk-footer">Footer noise</footer></body></html>"""

TEAM = """<html><body><header id="title-block-header"><h1 class="title">Vanshika Saraf</h1>
<div class="gcpp-hero__lede">Research Analyst for the Geostrategy Programme</div></header>
<main id="quarto-document-content"><p>Vanshika Saraf is a Research Analyst.</p>
<p><strong>Areas of research</strong></p>
<div class="research-categories"><div class="research-category"><p><a href="#">Indo-Pacific</a></p></div></div>
<div id="listing-recent-pubs" class="quarto-listing"><a href="../../content/publications/p1.html">
<div class="content-card" data-author-string="Vanshika Saraf"><h2 class="content-title">Pub One</h2></div></a></div>
<div id="listing-recent-news" class="quarto-listing"><div class="quarto-post" data-categories="%s">
<div class="body"><h3 class="listing-title"><a href="https://news.example/oped1">Op-ed One</a></h3></div>
<div class="metadata"><a href="https://news.example/oped1"><div class="listing-date">Sep 19, 2026</div>
<div class="listing-authors">Bhumika Sevkani,Vanshika Saraf</div><div class="listing-source">Deccan Herald</div></a></div>
</div></div></main></body></html>""" % base64.b64encode(quote("Geostrategy,Geopolitics").encode()).decode()


def test_publication_metadata():
    r = extract_page(PUB, "https://takshashila.org.in/content/publications/eu-rearm.html",
                     content_type="publication")
    assert r["title"] == "European Rearmament"
    assert r["subtitle"].startswith("Precursors")
    assert r["authors"] == ["Anushka Saxena", "Bee Cee"]
    assert r["author_urls"][0] == "https://takshashila.org.in/content/team/anushka-saxena.html"
    assert r["date"] == "2026-03-09"
    assert r["categories"] == ["Defence", "Indian Foreign Policy"]
    assert r["document_series"] == "Takshashila Discussion Document 2025-06"
    assert r["document_version"].startswith("Version 1.0")
    assert r["pdf_links"] == ["https://takshashila.org.in/content/publications/assets/eu-rearm.pdf"]
    assert "https://www.nato.int/x" in r["external_links"]


def test_body_text_is_clean_and_sectioned():
    r = extract_page(PUB, "https://takshashila.org.in/content/publications/eu-rearm.html")
    t = r["text"]
    assert "# Executive Summary" in t and "- Point one about ReArm." in t
    assert "noise" not in t.lower() and "var x" not in t and "Footer" not in t
    assert "AUTHOR" not in t          # header table is metadata, not body


def test_team_profile_relationships():
    r = extract_page(TEAM, "https://takshashila.org.in/content/team/vanshika-saraf.html", content_type="person")
    assert r["role"] == "Research Analyst for the Geostrategy Programme"
    assert r["research_areas"] == ["Indo-Pacific"]
    kinds = {w["kind"] for w in r["works"]}
    assert kinds == {"publication", "in_the_news"}
    oped = [w for w in r["works"] if w["kind"] == "in_the_news"][0]
    assert oped["authors"] == ["Bhumika Sevkani", "Vanshika Saraf"]
    assert oped["outlet"] == "Deccan Herald" and oped["date"] == "2026-09-19"
    assert oped["categories"] == ["Geostrategy", "Geopolitics"]
    assert "Areas of research" in r["text"] and "Pub One" in r["text"]


def test_listing_entries():
    entries = extract_listing_entries(TEAM, "https://takshashila.org.in/pages/news/")
    assert any(e["title"] == "Op-ed One" and e["url"] == "https://news.example/oped1" for e in entries)


def test_schemeless_listing_link_not_resolved_onto_our_domain():
    html = ('<main id="quarto-document-content"><div class="quarto-post"><h3 class="listing-title">'
            '<a href="www.moneycontrol.com/news/opinion/x-1.html">X</a></h3></div></main>')
    e = extract_listing_entries(html, "https://takshashila.org.in/pages/news/")
    assert e[0]["url"] == "https://www.moneycontrol.com/news/opinion/x-1.html"
