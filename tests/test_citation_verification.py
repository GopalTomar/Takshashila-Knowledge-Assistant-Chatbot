"""Claim-level citation verification and the shared citation format."""

from src.citation_format import best_excerpt, build_citations
from src.citations import normalize_citations, verify

SRC = [
    {"title": "Review Rule", "text": "Any staff member can raise a red flag on a draft before publication. "
                                     "A red flag pauses release until the programme head reviews it.",
     "url": "https://commit.example/flag", "source": "commit_kb", "content_type": "playbook"},
    {"title": "Satellites", "text": "Remote sensing satellites support border monitoring and disaster response.",
     "url": "https://takshashila.org.in/geo", "source": "website", "authors": ["Y Nithiyanandam"],
     "date": "2025-02-10", "content_type": "publication"},
]


def test_grouped_and_bare_citations_normalised():
    assert normalize_citations("x [Source 1, 2] y") == "x [Source 1][Source 2] y"
    assert normalize_citations("x [Sources 1 and 2]") == "x [Source 1][Source 2]"
    assert normalize_citations("claim [1].") == "claim [Source 1]."


def test_supported_citations_kept_and_renumbered():
    ans = "Remote sensing satellites help border monitoring [Source 2]. Staff can raise a red flag on a draft [Source 1]."
    v = verify(ans, SRC)
    assert v["grounded"] and v["cited"] == [2, 1]
    assert v["answer"].startswith("Remote sensing satellites help border monitoring [Source 1].")
    assert [s["title"] for s in v["sources"]] == ["Satellites", "Review Rule"]


def test_fabricated_citation_number_removed():
    v = verify("Staff can raise a red flag on a draft [Source 7].", SRC)
    assert v["invalid_citations"] == [7]
    assert "[Source 7]" not in v["answer"]


def test_wrong_citation_is_dropped():
    # Claim about flags cited to the satellites passage → unsupported.
    v = verify("Staff can raise a red flag that pauses publication of a draft [Source 2].", SRC)
    assert v["unsupported_citations"] and v["unsupported_citations"][0]["source"] == 2
    assert not v["grounded"] and v["sources"] == []


def test_mixed_support_keeps_only_valid_source():
    ans = ("A red flag pauses release until the programme head reviews it [Source 1][Source 2]. "
           "Any staff member can raise a red flag [Source 1].")
    v = verify(ans, SRC)
    assert v["grounded"] and [s["title"] for s in v["sources"]] == ["Review Rule"]
    assert "[Source 2]" not in v["answer"]


def test_uncited_answer_attributed_only_to_overlapping_source():
    v = verify("Remote sensing satellites support border monitoring and disaster response.", SRC)
    assert v["grounded"] and v["attribution"] == "inferred"
    assert [s["title"] for s in v["sources"]] == ["Satellites"]


def test_uncited_hallucination_refused():
    v = verify("The institution was founded on Mars in 1850 by astronauts.", SRC)
    assert not v["grounded"] and v["sources"] == []


def test_build_citations_fields_and_excerpt():
    ans = "Satellites support border monitoring [Source 1]."
    c = build_citations([SRC[1]], ans)[0]
    assert c["n"] == 1 and c["title"] == "Satellites" and c["url"].startswith("https://")
    assert c["authors"] == ["Y Nithiyanandam"] and c["date"] == "2025-02-10"
    assert c["content_type_label"] == "Publication" and c["access"] == "public"
    assert build_citations([SRC[0]])[0]["access"] == "internal"
    assert "border monitoring" in c["excerpt"]


def test_best_excerpt_picks_matching_window():
    text = "Intro sentence here. " * 30 + "The key finding concerns semiconductor fabs. " + "Tail. " * 30
    ex = best_excerpt(text, "semiconductor fabs finding")
    assert "semiconductor fabs" in ex and len(ex) <= 310


def test_fullwidth_brackets_from_some_models():
    assert normalize_citations("claim【Source 2】. other【Source 1, 2】") == \
        "claim[Source 2]. other[Source 1][Source 2]"
    v = verify("Remote sensing satellites support border monitoring【Source 2】.", SRC)
    assert v["attribution"] == "cited" and v["answer"].endswith("[Source 1].")


def test_removed_citation_leaves_no_space_before_punctuation():
    v = verify("Staff can raise a red flag on drafts [Source 1]. A claim about satellites and borders\u202f[Source 1];"
               " border monitoring uses satellites [Source 2].", SRC)
    assert " ;" not in v["answer"] and "\u202f;" not in v["answer"]
