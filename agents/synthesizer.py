"""Synthesizer agent (BLUEPRINT.md §4.1, §12): writes only from the claims
it's given, composed per sub-question/section so context stays bounded as
the claim count grows. Citation integrity: the model only ever sees
small per-call indices, never asked to type a real claim_id — the caller
resolves indices to real IDs in code (BLUEPRINT.md §2.4), and a sentence
with no resolvable citation is dropped rather than rendered as fact.

Produces DRAFT sentences only — persistence and audit_passed/audit_reason
are owned by the Report Auditor (Phase 4), which runs next and is the
single source of truth for what actually reaches the report."""

from __future__ import annotations

from agents.schemas import SynthesizerOutput
from app.deps import Deps
from app.state import ClaimState, ContradictionState, ReportSentenceState, SubQuestionState
from core.logging import get_logger

logger = get_logger(__name__)


async def synthesize(
    deps: Deps,
    run_id: str,
    sub_questions: list[SubQuestionState],
    claims: list[ClaimState],
    contradictions: list[ContradictionState] | None = None,
) -> list[ReportSentenceState]:
    contradictions = contradictions or []
    claim_by_id = {c["id"]: c for c in claims}
    contradictions_by_subq: dict[str, list[ContradictionState]] = {}
    for contradiction in contradictions:
        contradictions_by_subq.setdefault(contradiction["subq_id"], []).append(contradiction)

    by_subq: dict[str, list[ClaimState]] = {}
    for claim in claims:
        by_subq.setdefault(claim["subq_id"], []).append(claim)

    sentences: list[ReportSentenceState] = []
    order = 0

    for subq in sub_questions:
        subq_claims = by_subq.get(subq["id"], [])
        if not subq_claims:
            sentences.append(
                ReportSentenceState(
                    section=subq["text"],
                    order=order,
                    text="Insufficient evidence was found for this sub-question.",
                    claim_ids=[],
                    audit_passed=False,
                    audit_reason="no verified claims for this sub-question",
                )
            )
            order += 1
            continue

        claim_block = "\n".join(f"[{i}] {c['text']}" for i, c in enumerate(subq_claims))
        try:
            output: SynthesizerOutput = await deps.mesh.complete_json(
                node="synthesizer",
                prompt_name="synthesizer.v1",
                variables={"sub_question": subq["text"], "claims": claim_block},
                schema=SynthesizerOutput,
                model_role="synth",
                run_id=run_id,
            )
        except Exception:
            logger.warning("synthesizer call failed for a sub-question; section omitted", exc_info=True)
            continue

        for section in output.sections:
            for sent in section.sentences:
                claim_ids = [
                    subq_claims[i]["id"] for i in sent.claim_indices if 0 <= i < len(subq_claims)
                ]
                if not claim_ids:
                    continue  # never render a sentence with no real citation (§2.2/§2.4)
                sentences.append(
                    ReportSentenceState(
                        section=section.section or subq["text"],
                        order=order,
                        text=sent.text,
                        claim_ids=claim_ids,
                        audit_passed=False,  # Phase 4 Auditor decides this
                        audit_reason=None,
                    )
                )
                order += 1

        # "Sources disagree" — deterministic, not model-generated: quotes
        # both conflicting claims verbatim rather than silently picking
        # one (BLUEPRINT.md §4.1 Contradiction Checker / Synthesizer).
        for contradiction in contradictions_by_subq.get(subq["id"], []):
            claim_a = claim_by_id.get(contradiction["claim_a"])
            claim_b = claim_by_id.get(contradiction["claim_b"])
            if not claim_a or not claim_b:
                continue
            sentences.append(
                ReportSentenceState(
                    section=f"{subq['text']} — Sources disagree",
                    order=order,
                    text=(
                        f"Sources disagree: one source states \"{claim_a['text']}\", while another states "
                        f"\"{claim_b['text']}\" ({contradiction['explanation']})."
                    ),
                    claim_ids=[claim_a["id"], claim_b["id"]],
                    audit_passed=False,
                    audit_reason=None,
                )
            )
            order += 1

    if sentences:
        deps.repo.create_report_sentences(run_id, sentences)
    return sentences
