#!/usr/bin/env python3
"""Citation support scorer — claim vs provided source texts (no external fetch by default).

Deterministic, dependency-free MVP for x402 paid route / free demo.
Does not invent sources. Scores lexical + token overlap only.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

_WORD = re.compile(r"[a-z0-9][a-z0-9\-]{1,40}", re.I)
_STOP = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "at", "by",
    "for", "with", "about", "against", "between", "into", "through", "during",
    "before", "after", "above", "below", "to", "from", "up", "down", "in", "out",
    "on", "off", "over", "under", "again", "further", "once", "here", "there",
    "all", "any", "both", "each", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very", "can",
    "will", "just", "don", "should", "now", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "of", "as", "it",
    "this", "that", "these", "those", "i", "you", "he", "she", "we", "they",
}


def tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _coverage(claim_tokens: set[str], source_tokens: set[str]) -> float:
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & source_tokens) / len(claim_tokens)


def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    return {(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)}


@dataclass
class SourceHit:
    index: int
    title: str
    coverage: float
    jaccard: float
    bigram_overlap: float
    score: float
    matched_terms: list[str]


def score_claim(
    claim: str,
    sources: Iterable[dict],
    *,
    min_support: float = 0.35,
) -> dict:
    """Score whether `claim` is supported by `sources`.

    Each source: {title?, text|body|content, url?}
    """
    claim = (claim or "").strip()
    claim_toks = tokenize(claim)
    claim_set = set(claim_toks)
    claim_bi = _bigrams(claim_toks)

    hits: list[SourceHit] = []
    src_list = list(sources or [])
    for i, src in enumerate(src_list):
        if not isinstance(src, dict):
            continue
        text = str(src.get("text") or src.get("body") or src.get("content") or "")
        title = str(src.get("title") or src.get("url") or f"source_{i}")
        stoks = tokenize(f"{title} {text}")
        sset = set(stoks)
        cov = _coverage(claim_set, sset)
        jac = _jaccard(claim_set, sset)
        bi = _jaccard(claim_bi, _bigrams(stoks)) if claim_bi else 0.0
        # weighted blend
        sc = 0.55 * cov + 0.25 * jac + 0.20 * bi
        matched = sorted(claim_set & sset)[:24]
        hits.append(
            SourceHit(
                index=i,
                title=title[:200],
                coverage=round(cov, 4),
                jaccard=round(jac, 4),
                bigram_overlap=round(bi, 4),
                score=round(sc, 4),
                matched_terms=matched,
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    best = hits[0].score if hits else 0.0
    # soft-OR: diminishing combine of top-3
    top = [h.score for h in hits[:3]]
    combined = 0.0
    for s in top:
        combined = combined + s * (1.0 - combined)

    if not claim:
        label = "invalid_claim"
    elif not hits:
        label = "no_sources"
    elif combined >= max(min_support, 0.55):
        label = "supported"
    elif combined >= min_support:
        label = "partial"
    else:
        label = "unsupported"

    return {
        "claim": claim[:2000],
        "label": label,
        "support_score": round(combined, 4),
        "best_source_score": round(best, 4),
        "min_support": min_support,
        "claim_token_count": len(claim_set),
        "sources_evaluated": len(hits),
        "top_sources": [
            {
                "index": h.index,
                "title": h.title,
                "score": h.score,
                "coverage": h.coverage,
                "jaccard": h.jaccard,
                "bigram_overlap": h.bigram_overlap,
                "matched_terms": h.matched_terms,
            }
            for h in hits[:5]
        ],
        "method": "lexical_overlap_v1",
        "disclaimer": "Deterministic lexical scorer — not a full NLI model. Provide source texts; no silent web fetch.",
    }
