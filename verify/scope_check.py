"""Verifier stage 4b (closes the "partial loophole" found by the evaluation —
EVAL.md failure gallery #1–#7): a claim can quietly WIDEN the scope of what
its source says ("one retailer slowed" -> "consumer spending across the
entire economy slowed") and still be judged `partial`, which passes.

Rule: if a claim uses a scope-widening word (overall, entire, national,
global, every, all, most, always, never, ...), that word — or a listed
synonym — must also appear in the cited passage. Otherwise the claim fails
with stage_failed="scope_widening", even if the model called it
supported/partial. Deterministic, no LLM call, readable word lists below.

Deliberately checks the whole cited passage, not only the quoted span: the
model often quotes the single best sentence while the qualifier ("worldwide")
sits in the sentence before it, and rejecting on that would raise the
false-rejection rate. It is still strict enough to catch the planted cases,
where the widening word appears nowhere in the passage.
"""

from __future__ import annotations

import re

# Each group is a set of interchangeable words: one of them present in the
# claim requires one of them present in the passage.
SCOPE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"overall", "total", "aggregate"}),
    frozenset({"entire", "entirely", "whole"}),
    frozenset({"national", "nationwide", "countrywide", "country"}),
    frozenset({"global", "globally", "worldwide", "world", "international"}),
    frozenset({"every", "each", "all"}),
    frozenset({"most", "majority", "mostly"}),
    frozenset({"always", "invariably"}),
    frozenset({"never"}),
    frozenset({"widespread", "widely", "universal", "universally"}),
)
_WORD_RE = re.compile(r"[a-z]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def unsupported_scope_words(claim_text: str, passage_text: str) -> list[str]:
    """Scope-widening words in the claim with no counterpart in the passage."""
    claim_words, passage_words = _words(claim_text), _words(passage_text)
    missing: list[str] = []
    for group in SCOPE_GROUPS:
        used = group & claim_words
        if used and not (group & passage_words):
            missing.extend(sorted(used))
    return missing


def scope_consistent(claim_text: str, passage_text: str) -> tuple[bool, list[str]]:
    missing = unsupported_scope_words(claim_text, passage_text)
    return not missing, missing
