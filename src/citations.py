"""
citations.py — Post-generation citation verification (anti-hallucination).

The LLM answers only from numbered context passages and cites them inline as
``[Source N]``. Before an answer is shown this module enforces:

1. **Normalisation** — grouped forms (``[Source 1, 3]``, ``[Sources 1 and 2]``,
   ``[1][2]``) become individual ``[Source N]`` markers.
2. **No fabricated citations** — a number with no matching passage is removed
   and recorded in ``invalid_citations``.
3. **Claim-level support** — each sentence's content words are compared with the
   text (and metadata header) of every passage it cites; a citation whose passage
   does not support the sentence (overlap < ``min_claim_support``) is removed and
   recorded in ``unsupported_citations``.
4. **Only used sources are shown, renumbered in order**, so ``[Source 1]`` in the
   answer is always the first displayed source.
5. **Grounding decision** — an answer whose cited sentences are mostly
   unsupported, or that cites nothing and overlaps the context too little, is
   flagged ungrounded; the pipeline then returns the insufficient-evidence reply.
   An uncited but grounded answer is attributed only to passages that actually
   overlap it — never blindly to the top retrieved chunk.

Deterministic, dependency-free, no extra model calls.
"""

from __future__ import annotations

import re
from typing import Dict, List

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of",
    "to", "in", "on", "at", "by", "is", "are", "was", "were", "be", "been",
    "being", "as", "that", "this", "these", "those", "it", "its", "with",
    "from", "into", "about", "which", "who", "whom", "whose", "what", "when",
    "where", "how", "why", "will", "would", "can", "could", "should", "may",
    "might", "must", "not", "no", "do", "does", "did", "has", "have", "had",
    "they", "them", "their", "we", "our", "you", "your", "he", "she", "his",
    "her", "also", "there", "here", "than", "such", "so", "some", "any",
    "source", "sources", "answer", "details", "according", "based", "context",
    "takshashila", "institution", "provided", "mentioned", "states", "notes",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CITE_RE = re.compile(r"\[\s*Source\s*(\d+)\s*\]", re.IGNORECASE)
_GROUP_RE = re.compile(
    r"\[\s*(?:Sources?\s*)?(\d+(?:\s*(?:,|;|&|and)\s*(?:Source\s*)?\d+)+)\s*\]", re.IGNORECASE)
_BARE_RE = re.compile(r"\[(\d{1,2})\](?!\()")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*\-\[(])|\n+")

DEFAULT_MIN_CLAIM_SUPPORT = 0.25
MIN_GROUNDED_CLAIM_RATIO = 0.5


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 1]


def _token_set(text: str) -> set:
    return set(_tokens(text))


def normalize_citations(answer_text: str) -> str:
    """Expand grouped/bare citation forms into individual [Source N] markers."""
    def expand(m: "re.Match") -> str:
        nums = re.findall(r"\d+", m.group(1))
        return "".join(f"[Source {n}]" for n in nums)
    # Some models emit full-width / CJK brackets: 【Source 1】, ［Source 1］, 〔Source 1〕.
    s = re.sub(r"[【［〔〖]\s*((?:Sources?\s*)?\d+(?:\s*(?:,|;|&|and|、)\s*(?:Source\s*)?\d+)*)\s*[】］〕〗]",
               lambda m: "[" + m.group(1).replace("、", ",") + "]", answer_text or "")
    s = _GROUP_RE.sub(expand, s)
    s = re.sub(r"\[\s*Sources?\s*(\d+)\s*\]", r"[Source \1]", s, flags=re.IGNORECASE)
    if not _CITE_RE.search(s):
        s = _BARE_RE.sub(r"[Source \1]", s)
    return s


def cited_indices(answer_text: str) -> List[int]:
    """Distinct 1-based source numbers cited, in first-seen order."""
    seen, out = set(), []
    for m in _CITE_RE.finditer(normalize_citations(answer_text or "")):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def source_evidence_text(src: Dict) -> str:
    """What the model saw for a passage: metadata header + body."""
    bits = [src.get("title", ""), src.get("text", "")]
    for k in ("authors", "categories", "tags"):
        v = src.get(k)
        if isinstance(v, (list, tuple)):
            bits.append(" ".join(str(x) for x in v))
    for k in ("author", "date", "publication_date", "category", "section", "heading_path",
              "publisher", "role", "description", "url"):
        if src.get(k):
            bits.append(str(src[k]))
    return " ".join(bits)


def _support(sentence_tokens: set, source_tokens: set) -> float:
    if not sentence_tokens:
        return 1.0
    return len(sentence_tokens & source_tokens) / len(sentence_tokens)


def verify(answer_text: str, used_sources: List[Dict], *, min_overlap: float = 0.18,
           min_claim_support: float = DEFAULT_MIN_CLAIM_SUPPORT) -> Dict:
    """
    Verify + prune citations. ``used_sources[i]`` is ``[Source i+1]`` in the context.

    Returns {answer, sources, grounded, overlap, cited, invalid_citations,
             unsupported_citations, claims_total, claims_supported, grounding_score,
             dropped_uncited, attribution}.
    """
    text = normalize_citations(answer_text or "").strip()
    n = len(used_sources)
    src_tokens = [_token_set(source_evidence_text(s)) for s in used_sources]
    all_ctx = set().union(*src_tokens) if src_tokens else set()
    overlap = _support(_token_set(_CITE_RE.sub("", text)), all_ctx) if text else 0.0

    invalid = sorted({int(m.group(1)) for m in _CITE_RE.finditer(text) if not 1 <= int(m.group(1)) <= n})
    if invalid:
        text = _CITE_RE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n else "", text)

    # ── claim-level check ───────────────────────────────────────────────────────
    unsupported: List[Dict] = []
    claims_total = claims_supported = 0
    pieces = re.split(r"(\n+)", text)
    rebuilt = []
    for piece in pieces:
        if not piece or piece.startswith("\n"):
            rebuilt.append(piece)
            continue
        sentences = _SENT_SPLIT.split(piece)
        out_sents = []
        for sent in sentences:
            nums = [int(m.group(1)) for m in _CITE_RE.finditer(sent)]
            if not nums:
                out_sents.append(sent)
                continue
            claim_toks = _token_set(_CITE_RE.sub("", sent))
            substantive = len(claim_toks) >= 3
            keep_nums = []
            for k in dict.fromkeys(nums):
                sup = _support(claim_toks, src_tokens[k - 1])
                if not substantive or sup >= min_claim_support:
                    keep_nums.append(k)
                else:
                    unsupported.append({"source": k, "support": round(sup, 3), "claim": sent[:200]})
            if substantive:
                claims_total += 1
                claims_supported += bool(keep_nums)
            sent = _CITE_RE.sub(lambda m: m.group(0) if int(m.group(1)) in keep_nums else "", sent)
            # collapse duplicate adjacent markers of the same source
            sent = re.sub(r"(\[Source (\d+)\])(?:\s*\[Source \2\])+", r"\1", sent)
            out_sents.append(sent)
        rebuilt.append(" ".join(out_sents))
    text = "".join(rebuilt)

    cited = [k for k in dict.fromkeys(int(m.group(1)) for m in _CITE_RE.finditer(text))]
    grounding_score = (claims_supported / claims_total) if claims_total else (1.0 if cited else 0.0)

    if cited:
        remap = {orig: new for new, orig in enumerate(cited, start=1)}
        text = _CITE_RE.sub(lambda m: f"[Source {remap[int(m.group(1))]}]", text)
        text = re.sub(r"[^\S\n]{2,}", " ", text)                  # incl. NBSP / narrow spaces
        text = re.sub(r"[^\S\n]+([.,;:])", r"\1", text).strip()
        grounded = grounding_score >= MIN_GROUNDED_CLAIM_RATIO
        kept = [used_sources[k - 1] for k in cited]
        attribution = "cited"
    else:
        grounded = overlap >= min_overlap and not (claims_total and not claims_supported)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        # Attribute only to passages that genuinely overlap the answer (best first).
        ans_toks = _token_set(text)
        scored = sorted(((_support(ans_toks, t), i) for i, t in enumerate(src_tokens)),
                        key=lambda x: (-x[0], x[1]))
        kept = [used_sources[i] for s, i in scored if s >= min_claim_support][:3] if grounded else []
        if grounded and not kept:
            grounded = False
        attribution = "inferred" if kept else "none"

    return {
        "answer": text,
        "sources": kept,
        "grounded": grounded,
        "overlap": overlap,
        "cited": cited,
        "invalid_citations": invalid,
        "unsupported_citations": unsupported,
        "claims_total": claims_total,
        "claims_supported": claims_supported,
        "grounding_score": round(grounding_score, 3),
        "dropped_uncited": n - len(kept),
        "attribution": attribution,
    }
