"""Drafter agent (BLUEPRINT.md §4.1, §12): one fact per claim, drawn only
from the passages it's given. Passage selection is relevance-ranked and
capped at `max_passages_per_call` (never dumped); if more remain, the
Drafter is called in batches and the resulting claims are merged, so
context size stays bounded regardless of how much evidence a sub-question
accumulated."""

from __future__ import annotations

import asyncio
import uuid

from agents.schemas import DrafterOutput
from app.deps import Deps
from app.state import ClaimState, PassageState, SubQuestionState
from core.logging import get_logger
from core.relevance import top_k

logger = get_logger(__name__)


def _batches(items: list[PassageState], size: int) -> list[list[PassageState]]:
    if not items:
        return []
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _draft_for_subquestion(
    deps: Deps, run_id: str, subq: SubQuestionState, passages: list[PassageState]
) -> list[ClaimState]:
    if not passages:
        return []

    cap = deps.settings.max_passages_per_call
    query = f"{subq['text']} {' '.join(subq['acceptance_criteria'])}"
    ordered = top_k(query, passages, k=len(passages), key=lambda p: p["text"])

    claims: list[ClaimState] = []
    for batch in _batches(ordered, cap):
        passage_block = "\n\n".join(f"[{i}] {p['text']}" for i, p in enumerate(batch))
        try:
            output: DrafterOutput = await deps.mesh.complete_json(
                node="drafter",
                prompt_name="drafter.v1",
                variables={
                    "sub_question": subq["text"],
                    "acceptance_criteria": "; ".join(subq["acceptance_criteria"]),
                    "passages": passage_block,
                },
                schema=DrafterOutput,
                model_role="drafter",
                run_id=run_id,
            )
        except Exception:
            logger.warning("drafter call failed for a batch; continuing with other batches", exc_info=True)
            continue

        for draft_claim in output.claims:
            if not (0 <= draft_claim.passage_index < len(batch)):
                continue  # model cited an index we never offered — drop, don't guess
            passage = batch[draft_claim.passage_index]
            claims.append(
                ClaimState(
                    id=str(uuid.uuid4()),
                    subq_id=subq["id"],
                    text=draft_claim.text,
                    passage_id=passage["id"],
                    status="pending",
                )
            )

    return claims


async def draft_all(
    deps: Deps,
    run_id: str,
    sub_questions: list[SubQuestionState],
    passages: list[PassageState],
) -> list[ClaimState]:
    by_subq: dict[str, list[PassageState]] = {}
    for passage in passages:
        by_subq.setdefault(passage["subq_id"], []).append(passage)

    # Drafted concurrently across sub-questions (DESIGN.md §16) — same
    # latency rationale as agents/verifier.py.
    results = await asyncio.gather(
        *(_draft_for_subquestion(deps, run_id, subq, by_subq.get(subq["id"], [])) for subq in sub_questions)
    )
    all_claims: list[ClaimState] = [c for claims in results for c in claims]

    if all_claims:
        deps.repo.create_claims(run_id, all_claims)
    return all_claims
