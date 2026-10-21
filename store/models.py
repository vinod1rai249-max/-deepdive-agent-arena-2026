"""SQLAlchemy 2.x models for every entity in BLUEPRINT.md §7. SQLite via
plain `create_all` for the Core path — no Alembic (DESIGN.md §8: no live
data to migrate in a hackathon build)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    question: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Makes a run resumable after a process restart (BLUEPRINT.md §10/DESIGN.md §7):
    # on startup, find status="in_progress" runs and re-enter the graph after this node.
    last_completed_node: Mapped[str | None] = mapped_column(String, nullable=True)


class SubQuestion(Base):
    __tablename__ = "sub_questions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    text: Mapped[str] = mapped_column(String)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    time_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    premise_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    coverage: Mapped[float] = mapped_column(Float, default=0.0)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    subq_id: Mapped[str] = mapped_column(String, ForeignKey("sub_questions.id"))
    url: Mapped[str] = mapped_column(String)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    tier: Mapped[int] = mapped_column(Integer, default=4)
    content_hash: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    origin: Mapped[str] = mapped_column(String, default="web")


class Passage(Base):
    __tablename__ = "passages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(String, ForeignKey("sources.id"))
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String)


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    subq_id: Mapped[str] = mapped_column(String, ForeignKey("sub_questions.id"))
    text: Mapped[str] = mapped_column(String)
    passage_id: Mapped[str] = mapped_column(String, ForeignKey("passages.id"))
    status: Mapped[str] = mapped_column(String, default="pending")


class Verdict(Base):
    __tablename__ = "verdicts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    claim_id: Mapped[str] = mapped_column(String, ForeignKey("claims.id"))
    stage_reached: Mapped[str] = mapped_column(String)
    stage_failed: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String)
    entailment: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_span: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    passage_id: Mapped[str | None] = mapped_column(String, ForeignKey("passages.id"), nullable=True)
    model: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    confidence_level: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class Contradiction(Base):
    __tablename__ = "contradictions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    subq_id: Mapped[str] = mapped_column(String, ForeignKey("sub_questions.id"))
    claim_a: Mapped[str] = mapped_column(String, ForeignKey("claims.id"))
    claim_b: Mapped[str] = mapped_column(String, ForeignKey("claims.id"))
    explanation: Mapped[str] = mapped_column(String)


class ReportSentence(Base):
    __tablename__ = "report_sentences"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    section: Mapped[str] = mapped_column(String)
    order: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String)
    claim_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    audit_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    audit_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(String, ForeignKey("runs.id"), nullable=True)
    node: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    subq_id: Mapped[str] = mapped_column(String, ForeignKey("sub_questions.id"))
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    query: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    result_count: Mapped[int] = mapped_column(Integer, default=0)


class HumanReview(Base):
    """Human-in-the-loop override (BLUEPRINT.md §4.1a). A separate,
    additive record — it never mutates the original Verdict row, so both
    "what the AI judged" and "what a human decided, and why" stay visible
    side by side (the trail never silently overwrites itself)."""

    __tablename__ = "human_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"))
    claim_id: Mapped[str] = mapped_column(String, ForeignKey("claims.id"))
    previous_status: Mapped[str] = mapped_column(String)
    new_status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    reviewed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
