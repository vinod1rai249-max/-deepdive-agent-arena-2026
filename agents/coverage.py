"""Loop coverage judge (BLUEPRINT.md §6): the fraction of a sub-question's
acceptance criteria satisfied by its verified claims, weighted down when
the only support is low-confidence (low-tier or stale) evidence — not a
bare LLM self-report of "are we done yet.\""""

from __future__ import annotations

from agents.schemas import CoverageJudgment
from app.deps import Deps
from app.state import ClaimState, SubQuestionState, VerdictState


async def compute_coverage(
    deps: Deps,
    run_id: str,
    subq: SubQuestionState,
    verified_claims: list[ClaimState],
    verdicts_for_verified: list[VerdictState],
) -> tuple[float, str | None]:
    criteria = subq["acceptance_criteria"]
    if not criteria:
        return 1.0, None  # nothing to satisfy
    if not verified_claims:
        return 0.0, "no verified evidence yet"

    claim_block = "\n".join(f"- {c['text']}" for c in verified_claims)
    try:
        judgment: CoverageJudgment = await deps.mesh.complete_json(
            node="coverage",
            prompt_name="coverage.v1",
            variables={
                "sub_question": subq["text"],
                "acceptance_criteria": "\n".join(f"{i}. {ac}" for i, ac in enumerate(criteria)),
                "claims": claim_block,
            },
            schema=CoverageJudgment,
            model_role="coverage",
            run_id=run_id,
        )
    except Exception:
        return 0.0, "coverage judge call failed"

    # Defensive against a schema-valid but wrong-length list from the model.
    satisfied = (judgment.criteria_satisfied + [False] * len(criteria))[: len(criteria)]
    raw_coverage = sum(1 for s in satisfied if s) / len(criteria)

    confidences = [v["confidence_score"] for v in verdicts_for_verified if v.get("confidence_score") is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5

    weighted = raw_coverage * (0.5 + 0.5 * avg_confidence)
    return weighted, judgment.missing_aspect
