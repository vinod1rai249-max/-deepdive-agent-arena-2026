"""Working-memory state threaded through the LangGraph run (BLUEPRINT.md
§12 "Working memory"). Plain TypedDicts — distinct from the SQLAlchemy ORM
models in store/models.py, which are the persisted (long-term memory)
shape. Each node reads/writes this; the repo layer persists it."""

from __future__ import annotations

from typing import TypedDict


class SubQuestionState(TypedDict):
    id: str
    text: str
    acceptance_criteria: list[str]
    time_scope: str | None
    premise_flag: bool


class SourceState(TypedDict):
    id: str
    subq_id: str
    url: str
    domain: str | None
    title: str | None
    tier: int
    content_hash: str
    text: str
    origin: str
    published_at: str | None  # ISO-8601, or None if unknown


class PassageState(TypedDict):
    id: str
    source_id: str
    subq_id: str
    start: int
    end: int
    text: str


class ClaimState(TypedDict):
    id: str
    subq_id: str
    text: str
    passage_id: str
    status: str  # "pending" | "verified" | "partial" | "re_cited" | "rejected" | "unverified"


class VerdictState(TypedDict):
    claim_id: str
    stage_reached: str
    stage_failed: str | None
    outcome: str
    entailment: str | None
    evidence_span: str | None
    reason: str | None
    passage_id: str | None
    model: str  # "n/a" if the claim was rejected before any Mesh call (prefilter)
    prompt_version: str
    confidence_level: str | None
    confidence_score: float | None


# A claim in one of these statuses has passed verification (possibly after
# re-attribution) and may be used by the Synthesizer. Anything else
# ("pending", "rejected", "unverified") never reaches the report as fact
# (BLUEPRINT.md §2.2).
VERIFIED_STATUSES = frozenset({"verified", "partial", "re_cited"})


class ContradictionState(TypedDict):
    subq_id: str
    claim_a: str
    claim_b: str
    explanation: str


class ReportSentenceState(TypedDict):
    section: str
    order: int
    text: str
    claim_ids: list[str]
    audit_passed: bool
    audit_reason: str | None


class RunState(TypedDict):
    run_id: str
    question: str
    sub_questions: list[SubQuestionState]
    sources: list[SourceState]
    passages: list[PassageState]
    claims: list[ClaimState]
    verdicts: list[VerdictState]
    contradictions: list[ContradictionState]
    report_sentences: list[ReportSentenceState]

    # Loop engineering (BLUEPRINT.md §6)
    started_at: float  # time.monotonic() at run start, for the deadline stop_reason
    iteration: int
    max_iterations: int
    coverage: dict[str, float]  # subq_id -> weighted coverage fraction
    stop_reason: str | None  # "coverage_met" | "max_iter" | "no_new_evidence" | "budget" | None (still looping)
    research_targets: list[str]  # subq_ids to (re-)research this pass; [] means "all" (first pass)
    failure_reasons: dict[str, str]  # subq_id -> why it's thin, fed back into re-research query generation
    seen_content_hashes: list[str]  # global across iterations, for the no-new-evidence detector
    no_new_evidence_triggered: bool
