"""Shared lexical relevance scoring — BLUEPRINT.md §12.

Used wherever the codebase needs to cheaply rank candidate text against a
query without spending an LLM call: the Claim Verifier's pre-filter and
re-attribution ranking (§5 stages 0 and 5), Drafter passage selection, the
Evidence Curator's over-cap trimming, and Follow-up evidence retrieval
(§12). One scorer, reused everywhere, keeps the "no vector DB" decision
(DESIGN.md §9) consistent instead of quietly reintroducing semantic search
in just one corner of the codebase.

Deliberately simple: tokenized word-overlap (Jaccard) plus a bonus for
shared numbers/years — exactly the signal that matters for "is this
passage even about the same thing as this claim/question," without an
embedding model or any AI call (this module is pure code, by design: it
must stay usable from inside the Verifier's cheap pre-filter stage, which
exists specifically to avoid spending a Mesh call).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence, TypeVar

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")
_NUMBER_RE = re.compile(r"\d[\d,.]*")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is",
        "are", "was", "were", "be", "been", "being", "it", "its", "this",
        "that", "these", "those", "as", "by", "with", "at", "from",
        "which", "who", "what", "how", "but", "not", "no", "do", "does",
        "did", "has", "have", "had", "will", "would", "can", "could",
        "should", "may", "might", "than", "then", "so", "such", "into",
        "about", "over", "under", "between", "also",
    }
)

T = TypeVar("T")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text))


def score(query: str, candidate: str) -> float:
    """Cheap 0.0-1.0 relevance score: Jaccard word overlap, with a small
    bonus if query and candidate share at least one number/year (claims
    and passages are often distinguished by exactly that)."""
    q_tokens, c_tokens = _tokens(query), _tokens(candidate)
    if not q_tokens or not c_tokens:
        return 0.0

    overlap = len(q_tokens & c_tokens)
    union = len(q_tokens | c_tokens)
    jaccard = overlap / union if union else 0.0

    q_nums, c_nums = _numbers(query), _numbers(candidate)
    number_bonus = 0.1 if (q_nums and q_nums & c_nums) else 0.0

    return min(1.0, jaccard + number_bonus)


@dataclass
class Ranked:
    item: object
    text: str
    score: float


def rank(query: str, candidates: Sequence[T], key: Callable[[T], str] = lambda item: str(item)) -> list[Ranked]:
    """Rank `candidates` against `query` by relevance, highest first.
    `key(item)` extracts the text to score; defaults to `str(item)`."""
    ranked = [Ranked(item=item, text=key(item), score=score(query, key(item))) for item in candidates]
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def top_k(query: str, candidates: Sequence[T], k: int, key: Callable[[T], str] = lambda item: str(item)) -> list[T]:
    """Convenience: the top-k candidates by relevance to `query`. Used
    wherever a node must cap how much it feeds into a single prompt
    (BLUEPRINT.md §12) — Drafter passage selection, Curator trimming,
    Follow-up retrieval."""
    return [r.item for r in rank(query, candidates, key=key)[:k]]


def passes_prefilter(query: str, candidate: str, threshold: float = 0.05) -> bool:
    """Cheap gate used by the Claim Verifier's stage 0 (§5): True if
    `candidate` has any meaningful lexical overlap with `query`, False if
    it's obviously unrelated and not worth spending an LLM call on."""
    return score(query, candidate) >= threshold
