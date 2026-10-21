"""Verifier stage 6 (BLUEPRINT.md §5): f(verdict, source tier, count of
independent supporting sources, source age vs time_scope). Logic lives
here, in code, not a prompt — auditable, and it costs no Mesh call."""

from __future__ import annotations

import datetime as dt

_ENTAILMENT_BASE = {"supported": 1.0, "partial": 0.6, "re_cited": 0.8}
_TIER_FACTOR = {1: 1.0, 2: 0.9, 3: 0.75, 4: 0.5}


def score_confidence(
    *,
    entailment: str,
    tier: int,
    corroborating_sources: int = 1,
    published_at: dt.datetime | None = None,
    time_scope: str | None = None,
) -> tuple[str, float]:
    """Returns (level, numeric_score), level in {"high", "medium", "low"}."""
    base = _ENTAILMENT_BASE.get(entailment, 0.4)
    tier_factor = _TIER_FACTOR.get(tier, 0.5)
    corroboration_bonus = min(0.15, 0.05 * max(0, corroborating_sources - 1))

    recency_factor = 1.0
    if time_scope and published_at is None:
        recency_factor = 0.85  # the question cares about timing and we don't know when this was published

    numeric_score = min(1.0, base * tier_factor + corroboration_bonus) * recency_factor
    numeric_score = round(numeric_score, 3)

    if numeric_score >= 0.75:
        level = "high"
    elif numeric_score >= 0.5:
        level = "medium"
    else:
        level = "low"
    return level, numeric_score
