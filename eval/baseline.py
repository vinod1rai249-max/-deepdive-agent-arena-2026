"""Baseline comparator (BLUEPRINT.md §15): same Mesh model, a single
"answer with citations" prompt, no planning/verification/loop — the
thing DeepDive's multi-agent verification pipeline is supposed to beat.
Used to produce a side-by-side citation-precision comparison in
EVAL.md. Needs a real MESH_API_KEY to run; spends credits.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.schemas import SpanJudgment
from app.deps import Deps
from core.errors import GuardrailViolation, RetrievalError
from core.guardrails import wrap_untrusted
from retrieval.fetch_safe import assert_safe_url
from verify.numeric_check import numbers_consistent
from verify.span_match import span_exists


class BaselineSentence(BaseModel):
    text: str
    source_url: str = Field(description="The URL the model believes supports this sentence")


class BaselineAnswer(BaseModel):
    sentences: list[BaselineSentence]


async def answer_with_citations(deps: Deps, run_id: str, question: str) -> BaselineAnswer:
    """The baseline has no retrieval step and no code-level grounding
    check at all — it just asks the model to answer and cite a URL from
    its own knowledge, which is exactly the failure mode (a citation that
    doesn't genuinely trace to a checked source) the brief calls out as
    the thing every simple RAG wrapper gets wrong. That gap is the point:
    DeepDive's citations are never LLM-authored (BLUEPRINT.md §2.4);
    this baseline's citations always are."""
    return await deps.mesh.complete_json(
        node="baseline",
        prompt_name="baseline.v1",
        variables={"question": question},
        schema=BaselineAnswer,
        model_role="synth",
        run_id=run_id,
    )


async def score_baseline_answer(deps: Deps, answer: BaselineAnswer, run_id: str = "eval-baseline") -> dict[str, Any]:
    """What makes the baseline comparison in EVAL.md meaningful rather
    than just "here's some different text": each of the baseline's
    LLM-authored citations is actually fetched for real, then judged
    with the exact same span-existence + numeric-consistency check the
    Report Auditor applies to DeepDive's own sentences (`auditor` role —
    the same grading standard on both sides of the comparison). A
    citation whose URL doesn't even resolve, or whose content doesn't
    actually support the sentence once checked, counts as unsupported —
    which is exactly the failure mode this baseline exists to expose."""
    results: list[dict[str, Any]] = []

    for sentence in answer.sentences:
        try:
            assert_safe_url(sentence.source_url)
            page = await deps.search.scrape(sentence.source_url)
        except (GuardrailViolation, RetrievalError) as exc:
            results.append(
                {"text": sentence.text, "url": sentence.source_url, "url_fetchable": False, "supported": False, "reason": str(exc)}
            )
            continue

        if not page or not page.text:
            results.append(
                {"text": sentence.text, "url": sentence.source_url, "url_fetchable": False, "supported": False, "reason": "fetched page had no usable content"}
            )
            continue

        try:
            judgment: SpanJudgment = await deps.mesh.complete_json(
                node="baseline_grader",
                prompt_name="auditor.v1",
                variables={"sentence": sentence.text, "evidence": wrap_untrusted(page.text[:4000])},
                schema=SpanJudgment,
                model_role="auditor",
                run_id=run_id,
            )
        except Exception as exc:
            results.append(
                {"text": sentence.text, "url": sentence.source_url, "url_fetchable": True, "supported": False, "reason": f"grading call failed: {exc}"}
            )
            continue

        span_ok = span_exists(judgment.evidence_span, page.text)
        numbers_ok, _missing = numbers_consistent(sentence.text, judgment.evidence_span)
        supported = span_ok and numbers_ok and judgment.entailment in ("supported", "partial")
        results.append(
            {
                "text": sentence.text, "url": sentence.source_url, "url_fetchable": True, "supported": supported,
                "entailment": judgment.entailment, "reason": judgment.reason,
            }
        )

    precision = round(sum(1 for r in results if r["supported"]) / len(results), 4) if results else None
    return {"citation_precision": precision, "n_sentences": len(results), "details": results}
