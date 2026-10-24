"""Verifier stage 6 (BLUEPRINT.md §5): f(verdict, source tier, count of
independent supporting sources, source age vs time_scope). Logic lives
here, in code, not a prompt — auditable, and it costs no Mesh call."""

from __future__ import annotations

import datetime as dt
import re

_ENTAILMENT_BASE = {"supported": 1.0, "partial": 0.6, "re_cited": 0.8}
_TIER_FACTOR = {1: 1.0, 2: 0.9, 3: 0.75, 4: 0.5}


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
STALE_FACTOR = 0.8


def _target_year(time_scope: str | None) -> int | None:
    """Latest 4-digit year mentioned in a sub-question's time_scope
    ("2026", "2024-2026"); None for "current"/free text with no year."""
    if not time_scope:
        return None
    years = [int(m.group(0)) for m in _YEAR_RE.finditer(time_scope)]
    return max(years) if years else None


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
    elif time_scope and published_at is not None:
        target = _target_year(time_scope)
        if target is None and time_scope.strip().lower() == "current":
            target = dt.datetime.now().year
        if target is not None and published_at.year < target - 1:
            recency_factor = STALE_FACTOR  # source predates the window the question cares about by >1 year

    numeric_score = min(1.0, base * tier_factor + corroboration_bonus) * recency_factor
    numeric_score = round(numeric_score, 3)

    if numeric_score >= 0.75:
        level = "high"
    elif numeric_score >= 0.5:
        level = "medium"
    else:
        level = "low"
    return level, numeric_score
