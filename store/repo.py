"""Repository layer over SQLite (WAL mode). All node outputs persist through
here before a graph node advances — that's what makes a run replayable
(BLUEPRINT.md §12 "Replay mode") and resumable after a crash (§10)."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from store.models import (
    Base,
    Claim,
    Contradiction,
    HumanReview,
    LLMCall,
    Passage,
    ReportSentence,
    Run,
    Search,
    Source,
    SubQuestion,
    Verdict,
)


class Repo:
    def __init__(self, database_url: str = "sqlite:///./deepdive.db") -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine = create_engine(database_url, connect_args=connect_args)
        if database_url.startswith("sqlite"):
            with self.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        # expire_on_commit=False: callers read attributes off a Run/etc.
        # after its session has closed (e.g. `run.id` right after
        # create_run() returns) — with the default True, that access would
        # try to refresh from an already-closed session and raise
        # DetachedInstanceError.
        self.Session: sessionmaker[Session] = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_run(self, question: str, config_snapshot: dict[str, Any] | None = None) -> Run:
        with self.session() as session:
            run = Run(question=question, config_snapshot=config_snapshot or {})
            session.add(run)
            session.flush()
            run_id = run.id
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> Run | None:
        with self.session() as session:
            return session.get(Run, run_id)

    def mark_run_node(self, run_id: str, node: str) -> None:
        """Record the last node whose output was successfully persisted —
        the anchor a restart resumes from."""
        with self.session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.last_completed_node = node

    def find_in_progress_runs(self) -> list[Run]:
        with self.session() as session:
            return list(session.query(Run).filter(Run.status == "in_progress").all())

    def update_run_status(self, run_id: str, status: str) -> None:
        with self.session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = status
                if status in ("complete", "failed"):
                    run.finished_at = dt.datetime.now(dt.timezone.utc)

    def update_subquestion_coverage(self, coverage: dict[str, float]) -> None:
        """Persists the loop's computed coverage score per sub-question
        (BLUEPRINT.md §6) to the DB. Found missing during the first live
        run (DESIGN.md §16): `loop_control.decide()` always computed and
        used real coverage internally to drive the loop, but never wrote
        it back — every SubQuestion row showed 0.00 in the trail
        regardless of what the loop actually saw, which undermines "a
        human can see exactly why this run stopped" (§2.5)."""
        with self.session() as session:
            for subq_id, score in coverage.items():
                subq = session.get(SubQuestion, subq_id)
                if subq is not None:
                    subq.coverage = score

    def finalize_run(self, run_id: str, status: str, stop_reason: str | None = None, iterations: int | None = None) -> None:
        with self.session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = status
                run.finished_at = dt.datetime.now(dt.timezone.utc)
                if stop_reason is not None:
                    run.stop_reason = stop_reason
                if iterations is not None:
                    run.iterations = iterations
                total_cost = session.query(LLMCall).filter_by(run_id=run_id).all()
                run.total_cost_usd = sum(c.cost_usd for c in total_cost)

    def create_subquestions(self, run_id: str, sub_questions: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for sq in sub_questions:
                session.add(
                    SubQuestion(
                        id=sq["id"],
                        run_id=run_id,
                        text=sq["text"],
                        acceptance_criteria=sq["acceptance_criteria"],
                        time_scope=sq["time_scope"],
                        premise_flag=sq["premise_flag"],
                    )
                )

    def create_sources(self, run_id: str, sources: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for s in sources:
                published_at = None
                if s.get("published_at"):
                    try:
                        published_at = dt.datetime.fromisoformat(s["published_at"])
                    except ValueError:
                        published_at = None
                session.add(
                    Source(
                        id=s["id"],
                        run_id=run_id,
                        subq_id=s["subq_id"],
                        url=s["url"],
                        domain=s["domain"],
                        title=s["title"],
                        published_at=published_at,
                        tier=s["tier"],
                        content_hash=s["content_hash"],
                        text=s["text"],
                        origin=s["origin"],
                    )
                )

    def create_passages(self, passages: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for p in passages:
                session.add(
                    Passage(
                        id=p["id"],
                        source_id=p["source_id"],
                        start=p["start"],
                        end=p["end"],
                        text=p["text"],
                    )
                )

    def create_claims(self, run_id: str, claims: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for c in claims:
                session.add(
                    Claim(
                        id=c["id"],
                        run_id=run_id,
                        subq_id=c["subq_id"],
                        text=c["text"],
                        passage_id=c["passage_id"],
                        status="pending",
                    )
                )

    def create_report_sentences(self, run_id: str, sentences: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for s in sentences:
                session.add(
                    ReportSentence(
                        run_id=run_id,
                        section=s["section"],
                        order=s["order"],
                        text=s["text"],
                        claim_ids=s["claim_ids"],
                        audit_passed=s.get("audit_passed", False),
                        audit_reason=s.get("audit_reason"),
                    )
                )

    def create_verdicts(self, verdicts: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for v in verdicts:
                session.add(
                    Verdict(
                        claim_id=v["claim_id"],
                        stage_reached=v["stage_reached"],
                        stage_failed=v["stage_failed"],
                        outcome=v["outcome"],
                        entailment=v["entailment"],
                        evidence_span=v["evidence_span"],
                        reason=v["reason"],
                        passage_id=v["passage_id"],
                        model=v["model"],
                        prompt_version=v["prompt_version"],
                        confidence_level=v["confidence_level"],
                        confidence_score=v["confidence_score"],
                    )
                )

    def update_claim_statuses(self, claims: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for c in claims:
                claim = session.get(Claim, c["id"])
                if claim is not None:
                    claim.status = c["status"]
                    claim.passage_id = c["passage_id"]  # may change on re-attribution

    def override_claim_status(self, run_id: str, claim_id: str, new_status: str, reason: str) -> None:
        """Human-in-the-loop override (BLUEPRINT.md §4.1a): a human
        promotes a rejected/unverified/partial claim, or explicitly
        confirms the AI's verdict. Never mutates the original Verdict —
        records a separate HumanReview row so both the AI's original
        judgment and the human's decision stay visible."""
        with self.session() as session:
            claim = session.get(Claim, claim_id)
            if claim is None or claim.run_id != run_id:
                raise ValueError(f"no such claim {claim_id} in run {run_id}")
            previous_status = claim.status
            claim.status = new_status
            session.add(
                HumanReview(
                    run_id=run_id, claim_id=claim_id, previous_status=previous_status,
                    new_status=new_status, reason=reason,
                )
            )

    def get_human_reviews(self, run_id: str) -> list[dict[str, Any]]:
        with self.session() as session:
            reviews = (
                session.query(HumanReview).filter_by(run_id=run_id).order_by(HumanReview.reviewed_at).all()
            )
            return [
                {
                    "claim_id": r.claim_id, "previous_status": r.previous_status, "new_status": r.new_status,
                    "reason": r.reason, "reviewed_at": r.reviewed_at.isoformat(),
                }
                for r in reviews
            ]

    def clear_report_sentences(self, run_id: str) -> None:
        with self.session() as session:
            session.query(ReportSentence).filter_by(run_id=run_id).delete()

    def clear_contradictions(self, run_id: str) -> None:
        with self.session() as session:
            session.query(Contradiction).filter_by(run_id=run_id).delete()

    def create_contradictions(self, run_id: str, contradictions: list[dict[str, Any]]) -> None:
        with self.session() as session:
            for c in contradictions:
                session.add(
                    Contradiction(
                        run_id=run_id,
                        subq_id=c["subq_id"],
                        claim_a=c["claim_a"],
                        claim_b=c["claim_b"],
                        explanation=c["explanation"],
                    )
                )

    def get_report(self, run_id: str) -> list[dict[str, Any]]:
        """Final report sentences with resolved source title/url/tier per
        citation (BLUEPRINT.md §13 API contract) — a reader can verify
        "does this citation support this sentence" from this JSON alone,
        without touching the database (DESIGN.md §3)."""
        with self.session() as session:
            sentences = (
                session.query(ReportSentence)
                .filter_by(run_id=run_id, audit_passed=True)
                .order_by(ReportSentence.order)
                .all()
            )
            claims_by_id = {c.id: c for c in session.query(Claim).filter_by(run_id=run_id).all()}
            passages_by_id = {p.id: p for p in session.query(Passage).all()}
            sources_by_id = {s.id: s for s in session.query(Source).filter_by(run_id=run_id).all()}

            result: list[dict[str, Any]] = []
            for sent in sentences:
                citations: list[dict[str, Any]] = []
                seen_source_ids: set[str] = set()
                for claim_id in sent.claim_ids:
                    claim = claims_by_id.get(claim_id)
                    if not claim:
                        continue
                    passage = passages_by_id.get(claim.passage_id)
                    if not passage:
                        continue
                    source = sources_by_id.get(passage.source_id)
                    if not source or source.id in seen_source_ids:
                        continue
                    seen_source_ids.add(source.id)
                    citations.append(
                        {"source_id": source.id, "title": source.title, "url": source.url, "domain": source.domain, "tier": source.tier}
                    )
                result.append({"section": sent.section, "order": sent.order, "text": sent.text, "citations": citations})
            return result

    def get_trail(self, run_id: str) -> dict[str, Any]:
        """The full auditable decision trail (BLUEPRINT.md §2.5): every
        sub-question, search, claim, and verdict — passed or rejected,
        with the reason — plus LLM call cost/latency totals."""
        with self.session() as session:
            run = session.get(Run, run_id)
            subqs = session.query(SubQuestion).filter_by(run_id=run_id).all()
            searches = session.query(Search).filter_by(run_id=run_id).all()
            claims = session.query(Claim).filter_by(run_id=run_id).all()
            verdicts = (
                session.query(Verdict).join(Claim, Verdict.claim_id == Claim.id).filter(Claim.run_id == run_id).all()
            )
            contradictions = session.query(Contradiction).filter_by(run_id=run_id).all()
            llm_calls = session.query(LLMCall).filter_by(run_id=run_id).all()

            return {
                "run_id": run_id,
                "question": run.question if run else None,
                "status": run.status if run else None,
                "stop_reason": run.stop_reason if run else None,
                "iterations": run.iterations if run else None,
                "sub_questions": [
                    {"id": sq.id, "text": sq.text, "acceptance_criteria": sq.acceptance_criteria, "coverage": sq.coverage, "premise_flag": sq.premise_flag}
                    for sq in subqs
                ],
                "searches": [
                    {"subq_id": s.subq_id, "query": s.query, "provider": s.provider, "result_count": s.result_count, "iteration": s.iteration}
                    for s in searches
                ],
                "claims": [{"id": c.id, "subq_id": c.subq_id, "text": c.text, "status": c.status} for c in claims],
                "verdicts": [
                    {
                        "claim_id": v.claim_id, "outcome": v.outcome, "stage_reached": v.stage_reached,
                        "stage_failed": v.stage_failed, "entailment": v.entailment, "evidence_span": v.evidence_span,
                        "reason": v.reason, "confidence_level": v.confidence_level, "confidence_score": v.confidence_score,
                    }
                    for v in verdicts
                ],
                "contradictions": [
                    {"subq_id": c.subq_id, "claim_a": c.claim_a, "claim_b": c.claim_b, "explanation": c.explanation}
                    for c in contradictions
                ],
                "llm_calls": {
                    "count": len(llm_calls),
                    "total_cost_usd": round(sum(c.cost_usd for c in llm_calls), 6),
                    "total_input_tokens": sum(c.input_tokens for c in llm_calls),
                    "total_output_tokens": sum(c.output_tokens for c in llm_calls),
                    "by_node": {
                        node: sum(1 for c in llm_calls if c.node == node) for node in sorted({c.node for c in llm_calls})
                    },
                },
            }

    def export_run(self, run_id: str) -> dict[str, Any]:
        """Serializes a completed run's full state (BLUEPRINT.md §13
        "Replay mode"): every row needed to reconstruct its report and
        trail without touching Mesh again. Paired with `import_run`."""
        with self.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ValueError(f"no such run: {run_id}")

            return {
                "run": {
                    "id": run.id, "question": run.question, "status": run.status,
                    "stop_reason": run.stop_reason, "iterations": run.iterations,
                    "total_cost_usd": run.total_cost_usd, "config_snapshot": run.config_snapshot,
                },
                "sub_questions": [
                    {
                        "id": sq.id, "text": sq.text, "acceptance_criteria": sq.acceptance_criteria,
                        "time_scope": sq.time_scope, "premise_flag": sq.premise_flag, "coverage": sq.coverage,
                    }
                    for sq in session.query(SubQuestion).filter_by(run_id=run_id).all()
                ],
                "sources": [
                    {
                        "id": s.id, "subq_id": s.subq_id, "url": s.url, "domain": s.domain, "title": s.title,
                        "published_at": s.published_at.isoformat() if s.published_at else None, "tier": s.tier,
                        "content_hash": s.content_hash, "text": s.text, "origin": s.origin,
                    }
                    for s in session.query(Source).filter_by(run_id=run_id).all()
                ],
                "passages": [
                    # Passage has no subq_id column of its own (store/models.py) —
                    # PassageState's convenience denormalization is reconstructed
                    # here via the passage's source. Fixed as part of the human
                    # override feature (DESIGN.md §15): without this, re-synthesis
                    # couldn't rebuild full PassageState objects from the DB.
                    {"id": p.id, "source_id": p.source_id, "subq_id": subq_id, "start": p.start, "end": p.end, "text": p.text}
                    for p, subq_id in session.query(Passage, Source.subq_id)
                    .join(Source, Passage.source_id == Source.id)
                    .filter(Source.run_id == run_id)
                    .all()
                ],
                "claims": [
                    {"id": c.id, "subq_id": c.subq_id, "text": c.text, "passage_id": c.passage_id, "status": c.status}
                    for c in session.query(Claim).filter_by(run_id=run_id).all()
                ],
                "verdicts": [
                    {
                        "claim_id": v.claim_id, "stage_reached": v.stage_reached, "stage_failed": v.stage_failed,
                        "outcome": v.outcome, "entailment": v.entailment, "evidence_span": v.evidence_span,
                        "reason": v.reason, "passage_id": v.passage_id, "model": v.model,
                        "prompt_version": v.prompt_version, "confidence_level": v.confidence_level,
                        "confidence_score": v.confidence_score,
                    }
                    for v in session.query(Verdict)
                    .join(Claim, Verdict.claim_id == Claim.id)
                    .filter(Claim.run_id == run_id)
                    .all()
                ],
                "contradictions": [
                    {"subq_id": c.subq_id, "claim_a": c.claim_a, "claim_b": c.claim_b, "explanation": c.explanation}
                    for c in session.query(Contradiction).filter_by(run_id=run_id).all()
                ],
                "report_sentences": [
                    {
                        "section": r.section, "order": r.order, "text": r.text, "claim_ids": r.claim_ids,
                        "audit_passed": r.audit_passed, "audit_reason": r.audit_reason,
                    }
                    for r in session.query(ReportSentence).filter_by(run_id=run_id).all()
                ],
                "searches": [
                    {"subq_id": s.subq_id, "iteration": s.iteration, "query": s.query, "provider": s.provider, "result_count": s.result_count}
                    for s in session.query(Search).filter_by(run_id=run_id).all()
                ],
                "llm_calls": [
                    {
                        "node": c.node, "model": c.model, "prompt_version": c.prompt_version,
                        "input_tokens": c.input_tokens, "output_tokens": c.output_tokens, "cost_usd": c.cost_usd,
                        "latency_ms": c.latency_ms, "cached": c.cached, "error": c.error,
                    }
                    for c in session.query(LLMCall).filter_by(run_id=run_id).all()
                ],
            }

    def import_run(self, data: dict[str, Any]) -> str:
        """Loads an `export_run` dump into this DB as a fresh, independent
        run — the replay mechanism (BLUEPRINT.md §13): no Mesh key or
        credits needed to inspect it afterward."""
        run_data = data["run"]
        run_id = run_data["id"]
        with self.session() as session:
            session.add(Run(**run_data))
            for sq in data["sub_questions"]:
                session.add(SubQuestion(run_id=run_id, **sq))
            for s in data["sources"]:
                published_at = dt.datetime.fromisoformat(s["published_at"]) if s.get("published_at") else None
                fields = {k: v for k, v in s.items() if k != "published_at"}
                session.add(Source(run_id=run_id, published_at=published_at, **fields))
            for p in data["passages"]:
                # subq_id is export_run's convenience denormalization (see
                # above) — Passage itself has no such column.
                fields = {k: v for k, v in p.items() if k != "subq_id"}
                session.add(Passage(**fields))
            for c in data["claims"]:
                session.add(Claim(run_id=run_id, **c))
            for v in data["verdicts"]:
                session.add(Verdict(**v))
            for c in data["contradictions"]:
                session.add(Contradiction(run_id=run_id, **c))
            for r in data["report_sentences"]:
                session.add(ReportSentence(run_id=run_id, **r))
            for s in data["searches"]:
                session.add(Search(run_id=run_id, **s))
            for c in data["llm_calls"]:
                session.add(LLMCall(run_id=run_id, **c))
        return run_id

    def record_search(self, run_id: str, subq_id: str, query: str, provider: str, result_count: int, iteration: int = 0) -> None:
        with self.session() as session:
            session.add(
                Search(
                    run_id=run_id,
                    subq_id=subq_id,
                    iteration=iteration,
                    query=query,
                    provider=provider,
                    result_count=result_count,
                )
            )

    def record_llm_call(
        self,
        run_id: str | None,
        node: str,
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        cached: bool,
        error: str | None,
    ) -> None:
        with self.session() as session:
            session.add(
                LLMCall(
                    run_id=run_id,
                    node=node,
                    model=model,
                    prompt_version=prompt_version,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    cached=cached,
                    error=error,
                )
            )
