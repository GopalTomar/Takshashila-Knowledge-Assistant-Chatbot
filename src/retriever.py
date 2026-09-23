"""
retriever.py — Hybrid FAISS + BM25 retrieval with Reciprocal Rank Fusion.

Pipeline (unchanged architecture, hardened):
  1. FAISS semantic search (exact cosine over normalised embeddings).
  2. BM25 lexical search with punctuation-free tokens.
  3. Author-aware candidates: when the question names a known person (people
     graph), that person's documents are retrieved as an extra ranked list, so
     "What has X written about AI?" surfaces X's work, not generic AI pages.
  4. Reciprocal Rank Fusion of all lists (k=60).
  5. Boosts: source priority (Commit KB > website/local > other), exact title
     match, exact URL match, named author, and query content-type intent
     ("research areas", "op-eds", "internal playbook"…).
  6. Selection: evidence pages before navigation/listing pages, at most
     ``MAX_CHUNKS_PER_DOC`` chunks per document, a source-diversity guarantee
     (one slot for another relevant source), deterministic tie-breaking.

All filters (sources, category, author, year, content_type) apply to BOTH the
semantic and the lexical list. Every returned chunk carries a true cosine
``score`` against the query (BM25-only hits are scored too), which the evidence
gate and confidence tiers rely on.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

import numpy as np

from src import config
from src import vector_store
from src.utils import chunk_is_low_value, clean_chunk_metadata, get_logger

logger = get_logger("retriever", config.SCRAPE_LOG)

K_RRF = 60
MAX_CHUNKS_PER_DOC = 2
TITLE_MATCH_BOOST = 1.6
URL_MATCH_BOOST = 2.0
PERSON_BOOST = 1.35

_URL_IN_QUERY = re.compile(r"https?://\S+")

# Query wording that names a kind of content ("research areas", "op-eds",
# "courses"…) → boost documents of that content type. Mild, so topical relevance
# still dominates; it stops generic questions being answered from unrelated PDFs.
INTENT_BOOST = 1.3
_INTENT_RULES = [
    (re.compile(r"\bresearch (areas?|programmes?|programs?)\b|\bprogrammes?\b|\bfocus areas?\b", re.I),
     {"research_area", "programme"}),
    (re.compile(r"\bop-?eds?\b|\bopinion (pieces?|articles?)\b|\bin the news\b|\bcolumns?\b", re.I), {"op-ed"}),
    (re.compile(r"\bblogs?\b|\bblog posts?\b", re.I), {"blog"}),
    (re.compile(r"\bpublications?\b|\breports?\b|\bpapers?\b|\bbriefs?\b|\bdiscussion documents?\b", re.I),
     {"publication", "pdf"}),
    (re.compile(r"\bcourses?\b|\bpolicy school\b|\bgcpp\b|\bcertificate\b", re.I), {"course"}),
    (re.compile(r"\bevents?\b|\bconvenings?\b|\bworkshops?\b|\broundtables?\b", re.I), {"event"}),
    (re.compile(r"\bbooks?\b", re.I), {"book"}),
    (re.compile(r"\bpodcasts?\b", re.I), {"podcast"}),
    (re.compile(r"\bnewsletters?\b|\btrackers?\b|\bbulletins?\b", re.I), {"tracker", "newsletter"}),
    (re.compile(r"\bfellowships?\b", re.I), {"programme"}),
    (re.compile(r"\bteam\b|\bstaff\b|\bwho works\b|\bresearchers?\b|\bfaculty\b", re.I), {"person", "people_index"}),
    (re.compile(r"\bholidays?\b", re.I), {"holiday"}),
    (re.compile(r"\binternal\b|\bplaybook\b|\bdecisions?\b|\bhouse rules\b|\bnorms\b|\bcommit kb\b|"
                r"\bour (policy|policies|rules)\b|\bstaff (policy|policies|rules)\b", re.I),
     {"playbook", "decision", "insight", "idea", "note", "kb"}),
    (re.compile(r"\bcareers?\b|\bjobs? at\b|\bhiring\b|\binternships?\b", re.I), {"career"}),
    (re.compile(r"\bwhat is takshashila\b|\babout takshashila\b|\bmission\b", re.I), {"about"}),
]


_WORKS_Q = re.compile(r"\b(publications?|papers?|written|wrote|writings?|works?|op-?eds?|blogs?|articles?)\b"
                      r".*\bby\b|\bhas\b.*\b(written|published|authored)\b|\b(written|authored) by\b", re.I)


def query_intent_types(query: str) -> set:
    out = set()
    for rx, types in _INTENT_RULES:
        if rx.search(query or ""):
            out |= types
    return out


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip())


def ensure_bm25_ready():
    """Back-compat: BM25 is built with the serving state."""
    vector_store.load_index()


_ensure_bm25 = ensure_bm25_ready


def bm25_search(query: str, top_k: int = 20) -> List[Dict]:
    return vector_store.bm25_search(query, top_k=top_k)


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (t or "").lower())).strip()


def retrieve(
    query: str,
    top_k: int = config.TOP_K,
    source: Optional[str] = None,
    category: Optional[str] = None,
    author: Optional[str] = None,
    year: Optional[str] = None,
    use_hybrid: bool = True,
    content_type: Optional[str] = None,
    allowed_sources: Optional[Iterable[str]] = None,
    source_type: Optional[str] = None,            # legacy alias
    state=None,
) -> List[Dict]:
    """Hybrid retrieval. ``allowed_sources`` restricts results (access control)."""
    if source is None and source_type is not None:
        source = source_type
    query = normalize_query(query)
    if not query:
        return []
    st = state or vector_store.load_index()
    if st.ntotal == 0:
        return []

    # Effective source restriction = explicit filter ∩ access scope.
    scope = {s.lower() for s in allowed_sources} if allowed_sources else None
    if source and source != "all":
        eff_sources = {source.lower()} & scope if scope else {source.lower()}
        if not eff_sources:
            return []
    else:
        eff_sources = scope
    filt = dict(source_filter=eff_sources, category_filter=category, author_filter=author,
                year_filter=year, content_type_filter=content_type)

    from src.embeddings import embed_query
    qvec = embed_query(query)
    ranked_lists: List[List[Dict]] = []
    ranked_lists.append(vector_store.search(query, top_k=top_k * 3, state=st, query_vector=qvec, **filt))
    if use_hybrid:
        ranked_lists.append(vector_store.bm25_search(query, top_k=top_k * 3, state=st, **filt))

    people = []
    if not author:
        try:
            from src.people import detect_people
            people = detect_people(query)
        except Exception as exc:          # graph missing/corrupt → plain retrieval
            logger.debug(f"people detection skipped: {exc}")
    person_keys = set()
    for p in people[:3]:
        person_keys.add(p["name"].lower())
        pf = {**filt, "author_filter": p["name"]}
        ranked_lists.append(vector_store.search(query, top_k=top_k * 2, state=st, query_vector=qvec, **pf))
        if use_hybrid:
            ranked_lists.append(vector_store.bm25_search(query, top_k=top_k * 2, state=st, **pf))

    fused: Dict[int, float] = {}
    rows: Dict[int, Dict] = {}
    for lst in ranked_lists:
        for rank, r in enumerate(lst):
            row = r["_row"]
            fused[row] = fused.get(row, 0.0) + 1.0 / (K_RRF + rank + 1)
            if row not in rows or ("score" in r and "score" not in rows[row]):
                rows[row] = r

    q_title = _norm_title(query)
    q_urls = {u.rstrip(").,") for u in _URL_IN_QUERY.findall(query)}
    intent = query_intent_types(query) if not content_type else set()
    if intent:
        # Also pull candidates of the intended type(s), so they can be boosted even
        # when generic wording ranks them below the global top-k.
        tf = {**filt, "content_type_filter": ",".join(sorted(intent))}
        for lst in (vector_store.search(query, top_k=top_k * 2, state=st, query_vector=qvec, **tf),
                    vector_store.bm25_search(query, top_k=top_k * 2, state=st, **tf) if use_hybrid else []):
            for rank, r in enumerate(lst):
                row = r["_row"]
                fused[row] = fused.get(row, 0.0) + 1.0 / (K_RRF + rank + 1)
                rows.setdefault(row, r)
    for row, base in fused.items():
        ch = rows[row]
        tier = config.source_priority(ch.get("source") or ch.get("source_type") or "")
        boost = 1.0 + (tier - config.DEFAULT_SOURCE_PRIORITY) * config.SOURCE_PRIORITY_BOOST
        t = _norm_title(ch.get("title", ""))
        if len(t) >= 12 and t in q_title:
            boost *= TITLE_MATCH_BOOST
        if q_urls and (ch.get("url") in q_urls or ch.get("canonical_url") in q_urls):
            boost *= URL_MATCH_BOOST
        if intent and ch.get("content_type") in intent:
            boost *= INTENT_BOOST
        if person_keys:
            names = {str(a).lower() for a in (ch.get("authors") or [])}
            if names & person_keys or (ch.get("content_type") == "person"
                                       and ch.get("title", "").lower() in person_keys):
                boost *= PERSON_BOOST
        fused[row] = base * boost

    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))

    # True cosine for every candidate we might return (BM25-only hits included).
    def cosine(row: int) -> float:
        r = rows[row]
        if "score" in r:
            return float(r["score"])
        try:
            vec = st.index.reconstruct(int(row))
            return float(np.dot(vec, qvec[0]))
        except Exception:
            return 0.0

    doc_count: Dict[str, int] = {}
    final: List[Dict] = []
    taken = set()

    def select(low_value: bool) -> None:
        for row, rrf in ranked:
            if len(final) >= top_k:
                return
            if row in taken:
                continue
            ch = rows[row]
            if chunk_is_low_value(ch) != low_value:
                continue
            doc_id = ch.get("document_id") or ch.get("doc_id") or str(row)
            if doc_count.get(doc_id, 0) >= MAX_CHUNKS_PER_DOC:
                continue
            doc_count[doc_id] = doc_count.get(doc_id, 0) + 1
            taken.add(row)
            out = {k: v for k, v in ch.items() if k not in ("_row", "bm25_score")}
            out["score"] = cosine(row)
            out["rrf_score"] = rrf
            final.append(clean_chunk_metadata(out))

    select(low_value=False)
    if not final:
        select(low_value=True)

    # Source diversity: if every selected chunk comes from one source but another
    # allowed source has a genuinely relevant candidate, give it the last slot, so
    # questions spanning the Commit KB and the website see evidence from both.
    if len(final) >= 2 and len({c.get("source") for c in final}) == 1:
        only = final[0].get("source")
        for row, rrf in ranked:
            ch = rows[row]
            if row in taken or ch.get("source") == only or chunk_is_low_value(ch):
                continue
            cos = cosine(row)
            if cos >= config.MIN_SCORE_THRESHOLD:
                out = {k: v for k, v in ch.items() if k not in ("_row", "bm25_score")}
                out["score"], out["rrf_score"] = cos, rrf
                final[-1] = clean_chunk_metadata(out)
            break

    # "What has X written / publications by X": top-k cannot enumerate a body of work,
    # but X's profile lists it. Include the matching section of the profile.
    if person_keys and _WORKS_Q.search(query):
        have = {c.get("chunk_id") for c in final}
        want = ("op-eds" if re.search(r"op-?eds?|in the news|columns?", query, re.I) else
                "blog" if re.search(r"\bblogs?\b", query, re.I) else "publications")
        for idx, m in enumerate(st.metadata):
            if m.get("content_type") == "person" and m.get("title", "").lower() in person_keys \
                    and want in (m.get("heading_path") or "").lower() and m.get("chunk_id") not in have:
                out = dict(m)
                out["score"] = float(np.dot(st.index.reconstruct(idx), qvec[0]))
                out["rrf_score"] = 0.0
                final.append(clean_chunk_metadata(out))   # an extra slot, never displaced
                break

    return final


def best_cosine(chunks: List[Dict]) -> float:
    return max((float(c.get("score", 0.0)) for c in chunks), default=0.0)


def confidence_level(chunks: List[Dict]) -> str:
    top = best_cosine(chunks)
    if top >= config.CONF_HIGH_THRESHOLD:
        return "high"
    if top >= config.CONF_MEDIUM_THRESHOLD:
        return "medium"
    if top >= config.MIN_SCORE_THRESHOLD:
        return "low"
    return "none"


def has_sufficient_evidence(chunks: List[Dict]) -> bool:
    return best_cosine(chunks) >= config.MIN_SCORE_THRESHOLD
