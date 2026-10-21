"""Deduplication (BLUEPRINT.md §7/§9). Exact dedup is handled by the
Researcher via content hash before a Source is even built. This module is
the near-duplicate check the Evidence Curator runs across sources for the
same sub-question — catches the same article mirrored/syndicated across
two domains with minor formatting differences, where the content hash
differs but the substance doesn't."""

from __future__ import annotations


def _shingles(text: str, n: int = 5) -> set[str]:
    words = text.lower().split()
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def shingle_jaccard(a: str, b: str, n: int = 5) -> float:
    sa, sb = _shingles(a, n), _shingles(b, n)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_near_duplicate(a: str, b: str, threshold: float = 0.8, n: int = 5) -> bool:
    return shingle_jaccard(a, b, n) >= threshold
