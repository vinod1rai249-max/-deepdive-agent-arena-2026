"""Pydantic schemas for every Mesh call in the agent pipeline (BLUEPRINT.md
§1.2 — every call goes through MeshClient.complete_json with one of these).

Design note: claims and sentences cite by small per-call integer index
(`passage_index`, `claim_indices`), never by a real database ID. The model
is never given the opportunity to type a wrong or hallucinated ID — the
caller resolves index → real ID deterministically in code. This is the
same principle as BLUEPRINT.md §2.4 ("citations are never LLM-authored
strings"), applied one layer earlier.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SubQuestionOut(BaseModel):
    text: str
    acceptance_criteria: list[str] = Field(min_length=1, max_length=4)
    time_scope: str | None = None
    premise_flag: bool = False


class PlannerOutput(BaseModel):
    sub_questions: list[SubQuestionOut] = Field(min_length=1, max_length=7)


class QueryPlan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=4)


class DraftClaim(BaseModel):
    text: str
    passage_index: int


class DrafterOutput(BaseModel):
    claims: list[DraftClaim] = Field(default_factory=list)


class SynthesizedSentence(BaseModel):
    text: str
    claim_indices: list[int] = Field(min_length=1)


class SynthesizerSection(BaseModel):
    section: str
    sentences: list[SynthesizedSentence] = Field(default_factory=list)


class SynthesizerOutput(BaseModel):
    sections: list[SynthesizerSection] = Field(default_factory=list)


class SpanJudgment(BaseModel):
    """Claim Verifier stage 1 (BLUEPRINT.md §5): the model must quote the
    exact span it's relying on, which stage 2 then independently confirms
    actually exists in the passage — the model can't just assert
    entailment without showing its work."""

    evidence_span: str
    entailment: Literal["supported", "partial", "unsupported", "contradicted"]
    reason: str


class CoverageJudgment(BaseModel):
    """Loop coverage judge (BLUEPRINT.md §6): one bool per acceptance
    criterion, in order, plus the single biggest gap if not fully covered."""

    criteria_satisfied: list[bool]
    missing_aspect: str | None = None


class ContradictionPair(BaseModel):
    """Index-based, same principle as DraftClaim/SynthesizedSentence: the
    model points at two small per-call indices, never types a real
    claim_id (BLUEPRINT.md §2.4)."""

    claim_index_a: int
    claim_index_b: int
    explanation: str


class ContradictionCheckOutput(BaseModel):
    contradictions: list[ContradictionPair] = Field(default_factory=list)


class FollowupSentence(BaseModel):
    """Index-based citing again (BLUEPRINT.md §2.4): the model points at small
    per-call passage numbers, never types a source URL."""

    text: str
    passage_indices: list[int] = Field(min_length=1)


class FollowupOutput(BaseModel):
    sentences: list[FollowupSentence] = Field(default_factory=list)
