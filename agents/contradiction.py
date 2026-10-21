"""Contradiction Checker (BLUEPRINT.md §4.1 — Hardening bonus item):
flags where verified claims for the same sub-question genuinely disagree,
recording both sources rather than silently picking one. Operates only
on claims that already passed the Verifier (VERIFIED_STATUSES) — this is
about sources disagreeing with each other, not about unverified claims."""

from __future__ import annotations

import asyncio

from agents.schemas import ContradictionCheckOutput
from app.deps import Deps
from app.state import VERIFIED_STATUSES, ClaimState, ContradictionState, SubQuestionState
from core.logging import get_logger

logger = get_logger(__name__)


async def _check_subquestion(
    deps: Deps, run_id: str, subq: SubQuestionState, verified_claims: list[ClaimState]
) -> list[ContradictionState]:
    if len(verified_claims) < 2:
        return []

    claim_block = "\n".join(f"[{i}] {c['text']}" for i, c in enumerate(verified_claims))
    try:
        output: ContradictionCheckOutput = await deps.mesh.complete_json(
            node="contradiction",
            prompt_name="contradiction.v1",
            variables={"sub_question": subq["text"], "claims": claim_block},
            schema=ContradictionCheckOutput,
            model_role="cheap",
            run_id=run_id,
        )
    except Exception:
        logger.warning("contradiction check call failed", exc_info=True)
        return []

    results: list[ContradictionState] = []
    for pair in output.contradictions:
        a, b = pair.claim_index_a, pair.claim_index_b
        if a == b or not (0 <= a < len(verified_claims)) or not (0 <= b < len(verified_claims)):
            continue  # model pointed at an index we never offered, or paired a claim with itself — drop, don't guess
        results.append(
            ContradictionState(
                subq_id=subq["id"],
                claim_a=verified_claims[a]["id"],
                claim_b=verified_claims[b]["id"],
                explanation=pair.explanation,
            )
        )
    return results


async def check_all(
    deps: Deps, run_id: str, sub_questions: list[SubQuestionState], claims: list[ClaimState]
) -> list[ContradictionState]:
    by_subq: dict[str, list[ClaimState]] = {}
    for claim in claims:
        if claim["status"] in VERIFIED_STATUSES:
            by_subq.setdefault(claim["subq_id"], []).append(claim)

    # Checked concurrently across sub-questions (DESIGN.md §16) — same
    # latency rationale as agents/verifier.py.
    results = await asyncio.gather(
        *(_check_subquestion(deps, run_id, subq, by_subq.get(subq["id"], [])) for subq in sub_questions)
    )
    all_contradictions: list[ContradictionState] = [c for contradictions in results for c in contradictions]

    if all_contradictions:
        deps.repo.create_contradictions(run_id, all_contradictions)
    return all_contradictions
