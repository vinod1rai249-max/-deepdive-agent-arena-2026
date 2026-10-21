"""Loop-decision (BLUEPRINT.md §6): after each verification pass, decide
whether to loop back to the Researcher for specific thin sub-questions or
stop and hand off to the Synthesizer. All limits enforced here, in code
— never in a prompt.

Stop reasons: "coverage_met" | "max_iter" | "deadline" | "budget" | "no_new_evidence".
Budget is checked at this per-iteration granularity, recomputed from
real spend in the DB each time — not per individual Mesh call, which
stays an explicit Hardening-bucket item (BLUEPRINT.md §17 item 8).
"""

from __future__ import annotations

import asyncio
import time

from agents.coverage import compute_coverage
from app.deps import Deps
from app.state import VERIFIED_STATUSES, RunState
from core.budget import budget_exceeded

COVERAGE_THRESHOLD = 0.6


def _categorize_failure(verified_count: int, confidence_levels: set[str], missing_aspect: str | None) -> str:
    if verified_count == 0:
        return "all_rejected"
    if confidence_levels and confidence_levels <= {"low"}:
        return "low_tier_only"
    return missing_aspect or "missing_aspect"


async def _evaluate_subquestion(
    deps: Deps, run_id: str, subq: dict, claims: list[dict], verdicts_by_claim_id: dict
) -> tuple[str, float, str | None]:
    subq_claims = [c for c in claims if c["subq_id"] == subq["id"]]
    verified = [c for c in subq_claims if c["status"] in VERIFIED_STATUSES]
    verdicts_for_verified = [verdicts_by_claim_id[c["id"]] for c in verified if c["id"] in verdicts_by_claim_id]

    cov, missing_aspect = await compute_coverage(deps, run_id, subq, verified, verdicts_for_verified)

    failure_reason = None
    if cov < COVERAGE_THRESHOLD:
        levels = {v["confidence_level"] for v in verdicts_for_verified if v.get("confidence_level")}
        failure_reason = _categorize_failure(len(verified), levels, missing_aspect)

    return subq["id"], cov, failure_reason


async def decide(deps: Deps, run_id: str, state: RunState) -> dict:
    verdicts_by_claim_id = {v["claim_id"]: v for v in state["verdicts"]}

    # Evaluated concurrently across sub-questions (DESIGN.md §16) — same
    # latency rationale as agents/verifier.py.
    results = await asyncio.gather(
        *(
            _evaluate_subquestion(deps, run_id, subq, state["claims"], verdicts_by_claim_id)
            for subq in state["sub_questions"]
        )
    )

    coverage: dict[str, float] = {}
    failure_reasons: dict[str, str] = {}
    thin_subq_ids: list[str] = []
    for subq_id, cov, failure_reason in results:
        coverage[subq_id] = cov
        if failure_reason is not None:
            thin_subq_ids.append(subq_id)
            failure_reasons[subq_id] = failure_reason

    deps.repo.update_subquestion_coverage(coverage)

    next_iteration = state["iteration"] + 1
    stop_reason: str | None = None

    spent_usd, spent_tokens = deps.repo.get_run_cost_and_tokens(run_id)

    if not thin_subq_ids:
        stop_reason = "coverage_met"
    elif state.get("no_new_evidence_triggered"):
        stop_reason = "no_new_evidence"
    elif next_iteration >= state["max_iterations"]:
        stop_reason = "max_iter"
    elif time.monotonic() - state["started_at"] >= deps.settings.deadline_seconds_per_run:
        stop_reason = "deadline"
    elif budget_exceeded(spent_usd, spent_tokens, deps.settings.budget_usd_per_run, deps.settings.budget_tokens_per_run):
        stop_reason = "budget"

    if stop_reason:
        return {"coverage": coverage, "stop_reason": stop_reason, "research_targets": [], "failure_reasons": {}}

    return {
        "coverage": coverage,
        "stop_reason": None,
        "research_targets": thin_subq_ids,
        "failure_reasons": failure_reasons,
        "iteration": next_iteration,
    }
