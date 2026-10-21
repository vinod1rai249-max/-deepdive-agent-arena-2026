"""Verifier stage 4 (BLUEPRINT.md §5): even when entailment looks right, a
model can silently alter a specific number. This extracts numbers/
percentages/currency figures from the claim and confirms each survives
(after normalization) inside the cited evidence span.

Scoping note: a capitalized-phrase entity check was considered too (per
BLUEPRINT.md's original stage-4 description) but deliberately left out of
this hard gate. A regex-based entity extractor is unreliable enough
(e.g. any sentence-initial capitalized word looks like an "entity") that
using it to reject claims would directly hurt the false-rejection-rate
metric BLUEPRINT.md §15 tracks. Numbers are a much more reliable signal:
a claim either contains "42%" or it doesn't, and that's unambiguous to
check after normalization. If the eval numbers (§15) show entity drift is
a real problem worth the false-rejection risk, this is the place to add
it back with a proper check, not a crude regex gate.
"""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*\s*%?")


def _normalize_number(tok: str) -> str:
    tok = tok.strip()
    tok = tok.replace(",", "")
    tok = tok.replace(" ", "")
    if tok.endswith("."):
        tok = tok[:-1]
    return tok


def extract_numbers(text: str) -> set[str]:
    return {_normalize_number(m) for m in _NUMBER_RE.findall(text) if m.strip()}


def numbers_consistent(claim_text: str, span_text: str) -> tuple[bool, list[str]]:
    """True if every number in `claim_text` also appears in `span_text`
    (after normalization). A claim with no numbers trivially passes —
    this stage only ever catches a mismatch, never requires one."""
    claim_numbers = extract_numbers(claim_text)
    if not claim_numbers:
        return True, []
    span_numbers = extract_numbers(span_text)
    missing = sorted(n for n in claim_numbers if n not in span_numbers)
    return not missing, missing
