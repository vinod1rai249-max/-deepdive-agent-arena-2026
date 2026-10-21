"""Verifier stage 5 (BLUEPRINT.md §5): a claim can be true and well
supported but simply mis-cited by the Drafter — rejecting it outright
would conflate "false claim" with "correct claim, wrong footnote,"
understating groundedness and overstating the system's real error rate
(DESIGN.md §5). This ranks sibling passages for the same sub-question by
the shared relevance scorer (core/relevance.py) so the caller can retry
the span-quote judgment against the next most plausible passage before
giving up."""

from __future__ import annotations

from core.relevance import rank


def candidate_passages(claim_text: str, passages: list, exclude_id: str, k: int = 5) -> list:
    others = [p for p in passages if p["id"] != exclude_id]
    if not others:
        return []
    ranked = rank(claim_text, others, key=lambda p: p["text"])
    return [r.item for r in ranked[:k]]
