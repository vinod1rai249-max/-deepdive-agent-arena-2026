"""Report Auditor (BLUEPRINT.md §4.1, §5 — Phase 4). A second, different
model (the `auditor` role, never `verifier` — DESIGN.md §4) re-checks
every synthesized sentence against the passages behind the claims it
cites, applying the same span-existence and numeric-consistency code
checks as the Verifier (§5 stages 2/4). This is what catches the
Synthesizer combining or generalizing claims into something stronger
than any individual claim actually supports.

A sentence that fails gets one deterministic rewrite — falling back to
the verbatim text of the claims it cites, which already passed the
Verifier's own checks against this same evidence — and one re-audit
(BLUEPRINT.md: "max 2 rounds"). Still failing after that, it's stripped
from the rendered report, but never silently: every sentence's outcome,
passed or stripped, is persisted (BLUEPRINT.md §2.5 auditable trail).

A sentence with no citations at all is the deliberate "insufficient
evidence" placeholder the Synthesizer renders for an uncovered
sub-question (BLUEPRINT.md §2.3 "when evidence is thin, the report says
so") — there's nothing to fact-check there, so it passes through as-is.
"""

from __future__ import annotations

from agents.schemas import SpanJudgment
from app.deps import Deps
from app.state import ClaimState, PassageState, ReportSentenceState
from core.guardrails import wrap_untrusted
from core.logging import get_logger
from verify.gap_reason import reason_admits_gap
from verify.numeric_check import numbers_consistent
from verify.scope_check import scope_consistent
from verify.span_match import span_exists

logger = get_logger(__name__)

MAX_ROUNDS = 2


async def _judge(deps: Deps, run_id: str, sentence_text: str, evidence_text: str) -> SpanJudgment | None:
    try:
        return await deps.mesh.complete_json(
            node="auditor",
            prompt_name="auditor.v1",
            variables={"sentence": sentence_text, "evidence": wrap_untrusted(evidence_text)},
            schema=SpanJudgment,
            model_role="auditor",
            run_id=run_id,
        )
    except Exception:
        logger.warning("auditor call failed", exc_info=True)
        return None


def _passes(judgment: SpanJudgment, evidence_text: str, sentence_text: str) -> bool:
    span_ok = span_exists(judgment.evidence_span, evidence_text)
    numbers_ok, _missing = numbers_consistent(sentence_text, judgment.evidence_span)
    scope_ok, _missing_scope = scope_consistent(sentence_text, evidence_text)
    return (
        span_ok and numbers_ok and scope_ok and judgment.entailment in ("supported", "partial")
        and not reason_admits_gap(judgment.entailment, judgment.reason)
    )


async def audit_all(
    deps: Deps,
    run_id: str,
    draft_sentences: list[ReportSentenceState],
    claims: list[ClaimState],
    passages: list[PassageState],
) -> list[ReportSentenceState]:
    claim_by_id = {c["id"]: c for c in claims}
    passage_by_id = {p["id"]: p for p in passages}

    final_sentences: list[ReportSentenceState] = []
    all_results: list[ReportSentenceState] = []

    for sentence in draft_sentences:
        if not sentence["claim_ids"]:
            # The deliberate "insufficient evidence" placeholder — nothing
            # to audit, always kept as-is.
            result = {**sentence, "audit_passed": True, "audit_reason": "no citation — gap placeholder"}
            all_results.append(result)
            final_sentences.append(result)
            continue

        backing_claims = [claim_by_id[cid] for cid in sentence["claim_ids"] if cid in claim_by_id]
        evidence_texts: list[str] = []
        seen_passage_ids: set[str] = set()
        for claim in backing_claims:
            passage = passage_by_id.get(claim["passage_id"])
            if passage and passage["id"] not in seen_passage_ids:
                seen_passage_ids.add(passage["id"])
                evidence_texts.append(passage["text"])
        evidence_text = "\n\n".join(evidence_texts)

        text = sentence["text"]
        audit_passed = False
        audit_reason = "no evidence to audit against"

        for round_num in range(MAX_ROUNDS):
            if not evidence_text:
                break
            judgment = await _judge(deps, run_id, text, evidence_text)
            if judgment is None:
                audit_reason = "auditor call failed"
                break
            if _passes(judgment, evidence_text, text):
                audit_passed = True
                audit_reason = judgment.reason
                break
            audit_reason = judgment.reason
            if round_num == 0:
                # Deterministic rewrite: fall back to the claims verbatim.
                text = " ".join(c["text"] for c in backing_claims)

        result = {**sentence, "text": text, "audit_passed": audit_passed, "audit_reason": audit_reason}
        all_results.append(result)
        if audit_passed:
            final_sentences.append(result)
        else:
            logger.warning(
                "report auditor stripped a sentence",
                extra={"extra_fields": {"section": sentence["section"], "reason": audit_reason}},
            )

    if all_results:
        deps.repo.create_report_sentences(run_id, all_results)

    return final_sentences
