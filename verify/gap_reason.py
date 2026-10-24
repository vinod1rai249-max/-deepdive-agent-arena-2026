"""Verifier/Auditor stage 3b: a `partial` verdict whose OWN stated reason says
the evidence does not cover part of the sentence is not a pass.

Found by the evaluation (EVAL.md failure gallery #3, #4, #6): the Auditor
wrote reasons like "the evidence does not specifically mention dependency on
imports" or "does not link it to the Federal Reserve's decisions" and still
passed the sentence because `partial` is accepted. An independent grader
then marked those sentences partial/unsupported.

Rule: entailment == "partial" AND the reason contains a negated coverage
phrase ("does not mention/state/address/link/…", "no mention of …") -> fail
with stage_failed="self_reported_gap". Deterministic, no extra LLM call.

Deliberately narrow: it only touches `partial` verdicts. A `supported`
verdict whose reason merely says something unrelated is not mentioned is left
alone — on the evaluation data ~40% of reasons containing such a phrase were
still judged fully supported by the independent grader, so a blanket rule
would strip real findings (see EVAL.md §9 for the measured trade-off).
"""

from __future__ import annotations

import re

_GAP_RE = re.compile(
    r"\b(?:does|do|did|is|are|was|were)\s+not\s+(?:specifically\s+|explicitly\s+|directly\s+|clearly\s+|fully\s+)?"
    r"(?:mention|state|address|link|provide|say|specify|describe|discuss|confirm|establish|connect|relate|include|contain|show|support|indicate|cover)"
    r"|\bno\s+(?:explicit\s+|direct\s+|specific\s+)?(?:mention|reference|indication|link)\b",
    re.IGNORECASE,
)


def reason_admits_gap(entailment: str, reason: str | None) -> bool:
    return entailment == "partial" and bool(reason and _GAP_RE.search(reason))
