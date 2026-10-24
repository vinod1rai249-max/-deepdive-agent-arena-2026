"""Follow-up agent (BLUEPRINT.md §4.1, §12 "Episodic/follow-up memory"):
answers a new question from the passages a finished run already retrieved —
no new web search. Reuses the earlier research, and holds the answer to the
same standard as the report:

  1. pick the top-k stored passages by `core/relevance.py` (same scorer as
     everywhere; no embeddings, no vector DB);
  2. the `synth` role writes sentences that cite passages by index only;
  3. every sentence is re-verified with the Verifier's own pipeline — the
     `verifier` role must quote a span, code confirms the span exists in the
     cited passage, numbers match, and no scope-widening word is invented;
  4. sentences that fail are dropped (and counted); if nothing survives, the
     answer says the stored research does not contain it.

Citations are resolved from passage -> source in the database, never typed
by the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents.schemas import FollowupOutput, SpanJudgment
from app.deps import Deps
from core.guardrails import wrap_untrusted
from core.logging import get_logger
from core.relevance import top_k
from verify.gap_reason import reason_admits_gap
from verify.numeric_check import numbers_consistent
from verify.scope_check import scope_consistent
from verify.span_match import span_exists

logger = get_logger(__name__)

NO_EVIDENCE_ANSWER = "The stored research for this run does not contain enough verified evidence to answer that."


async def _verify_sentence(deps: Deps, run_id: str, text: str, passages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Returns the passage that verifiably supports `text`, or None."""
    for passage in passages:
        try:
            judgment: SpanJudgment = await deps.mesh.complete_json(
                node="followup_verifier", prompt_name="verifier.v1",
                variables={"claim": text, "passage": wrap_untrusted(passage["text"])},
                schema=SpanJudgment, model_role="verifier", run_id=run_id,
            )
        except Exception:
            logger.warning("follow-up verification call failed", exc_info=True)
            continue
        numbers_ok, _ = numbers_consistent(text, judgment.evidence_span)
        scope_ok, _ = scope_consistent(text, passage["text"])
        if (
            judgment.entailment in ("supported", "partial")
            and span_exists(judgment.evidence_span, passage["text"])
            and numbers_ok
            and scope_ok
            and not reason_admits_gap(judgment.entailment, judgment.reason)
        ):
            return passage
    return None


async def answer_followup(deps: Deps, run_id: str, question: str) -> dict[str, Any]:
    data = deps.repo.export_run(run_id)
    source_by_id = {s["id"]: s for s in data["sources"]}
    candidates = top_k(question, data["passages"], k=deps.settings.followup_top_k, key=lambda p: p["text"])
    if not candidates:
        return {"question": question, "answer": [], "dropped": 0, "note": NO_EVIDENCE_ANSWER}

    block = "\n\n".join(f"[{i}] {p['text']}" for i, p in enumerate(candidates))
    try:
        output: FollowupOutput = await deps.mesh.complete_json(
            node="followup", prompt_name="followup.v1",
            variables={"question": question, "passages": wrap_untrusted(block)},
            schema=FollowupOutput, model_role="synth", run_id=run_id,
        )
    except Exception:
        logger.warning("follow-up answer call failed", exc_info=True)
        return {"question": question, "answer": [], "dropped": 0, "note": "The follow-up answer could not be generated."}

    async def check(sentence) -> tuple[Any, dict[str, Any] | None]:
        cited = [candidates[i] for i in dict.fromkeys(sentence.passage_indices) if 0 <= i < len(candidates)]
        return sentence, (await _verify_sentence(deps, run_id, sentence.text, cited) if cited else None)

    checked = await asyncio.gather(*(check(s) for s in output.sentences))
    answer = []
    for sentence, passage in checked:
        if passage is None:
            continue
        source = source_by_id.get(passage["source_id"], {})
        answer.append(
            {"text": sentence.text, "citation": {"title": source.get("title"), "url": source.get("url"), "tier": source.get("tier")}}
        )
    dropped = len(output.sentences) - len(answer)
    return {"question": question, "answer": answer, "dropped": dropped, "note": None if answer else NO_EVIDENCE_ANSWER}
