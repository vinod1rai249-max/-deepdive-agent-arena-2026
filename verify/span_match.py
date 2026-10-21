"""Verifier stage 2 (BLUEPRINT.md §5): confirms the LLM's own quoted
evidence_span actually exists in the stored passage text. This is the
single check that stops a model from citing a source and inventing the
sentence anyway — it can lie about entailment, but it's much harder for
it to fabricate a verbatim span that also happens to fuzzy-match real
text it never saw (DESIGN.md §1)."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

SPAN_THRESHOLD_DEFAULT = 92


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def span_exists(span: str | None, passage: str, threshold: int = SPAN_THRESHOLD_DEFAULT) -> bool:
    if not span or not passage:
        return False
    return fuzz.partial_ratio(normalize(span), normalize(passage)) >= threshold
