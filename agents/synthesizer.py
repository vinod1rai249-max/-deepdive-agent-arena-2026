"""Synthesizer agent (BLUEPRINT.md §4.1, §12): writes only from the claims
it's given, composed per sub-question/section so context stays bounded as
the claim count grows. Citation integrity: the model only ever sees
small per-call indices, never asked to type a real claim_id — the caller
resolves indices to real IDs in code (BLUEPRINT.md §2.4), and a sentence
with no resolvable citation is dropped rather than rendered as fact.

Produces DRAFT sentences only — persistence and audit_passed/audit_reason
are owned by the Report Auditor (Phase 4), which runs next and is the
single source of truth for what actually reaches the report. Also appends
a deterministic "Gaps / Unverified" section (build_gaps_sentences)."""

from __future__ import annotations

from agents.schemas import SynthesizerOutput
from app.deps import Deps
from app.state import ClaimState, ContradictionState, ReportSentenceState, SubQuestionState, VerdictState
from core.logging import get_logger

logger = get_logger(__name__)

GAPS_SECTION = "Gaps / Unverified"
MAX_LISTED_UNVERIFIED = 10
THIN_COVERAGE_THRESHOLD = 0.6  # same τ as agents/loop_control.py::COVERAGE_THRESHOLD


def build_gaps_sentences(
    sub_questions: list[SubQuestionState],
    all_claims: list[ClaimState],
    verdicts: list[VerdictState],
    coverage: dict[str, float],
    start_order: int,
) -> list[ReportSentenceState]:
    """BLUEPRINT.md §2.3/§4.1: "when evidence is thin, the report says so".
    Deterministic — no LLM — so nothing here can be invented: every line
    is assembled from stored claim text, stored verdict reasons, and
    stored coverage numbers. Carries NO claim_ids on purpose: a rejected
    claim's cited source does not support it, so attaching that source as
    a citation would be exactly the failure this system exists to prevent.
    The Auditor passes citation-less sentences through as gap notes."""
    verdict_by_claim = {v["claim_id"]: v for v in verdicts}
    subq_text = {sq["id"]: sq["text"] for sq in sub_questions}
    sentences: list[ReportSentenceState] = []
    order = start_order

    def add(text: str) -> None:
        nonlocal order
        sentences.append(
            ReportSentenceState(section=GAPS_SECTION, order=order, text=text, claim_ids=[], audit_passed=False, audit_reason=None)
        )
        order += 1

    # The Planner flags questions that assume something that may be false
    # ("Since X happened, why..."). Surface that in the report itself — the
    # evaluation found the flag was set 5/5 but never shown to the reader.
    # Wording is deliberately modest: it says the premise was NOT assumed and
    # was not established, not that the premise is false.
    claims_by_subq: dict[str, int] = {}
    for c in all_claims:
        if c["status"] in ("verified", "partial", "re_cited"):
            claims_by_subq[c["subq_id"]] = claims_by_subq.get(c["subq_id"], 0) + 1
    for sq in sub_questions:
        if sq.get("premise_flag"):
            n_verified = claims_by_subq.get(sq["id"], 0)
            evidence = (
                f"{n_verified} verified claim(s) bear on it below" if n_verified
                else "no verified evidence either way was found"
            )
            add(
                f"Premise check: the question appears to assume something that may not be true. "
                f"The research did not take it for granted and instead asked \"{sq['text']}\" — {evidence}. "
                f"Read the findings below as answers to the facts that could be verified, not as confirmation of the premise."
            )

    for sq in sub_questions:
        cov = coverage.get(sq["id"])
        if cov is not None and cov < THIN_COVERAGE_THRESHOLD:
            add(f"Limited evidence for \"{sq['text']}\": only {cov:.0%} of its acceptance criteria were covered by verified claims.")

    excluded = [c for c in all_claims if c["status"] in ("rejected", "unverified")]
    for claim in excluded[:MAX_LISTED_UNVERIFIED]:
        v = verdict_by_claim.get(claim["id"])
        why = f"{v['stage_failed'] or v['outcome']}: {(v['reason'] or '').strip()[:200]}" if v else "no verdict recorded"
        add(
            f"Not verified, excluded from the findings above: \"{claim['text']}\" "
            f"(sub-question: {subq_text.get(claim['subq_id'], 'unknown')}; {claim['status']} — {why})."
        )
    if len(excluded) > MAX_LISTED_UNVERIFIED:
        add(f"{len(excluded) - MAX_LISTED_UNVERIFIED} further claims were also excluded; see the run trail for each one with its reason.")
    return sentences


async def synthesize(
    deps: Deps,
    run_id: str,
    sub_questions: list[SubQuestionState],
    claims: list[ClaimState],
    contradictions: list[ContradictionState] | None = None,
    all_claims: list[ClaimState] | None = None,
    verdicts: list[VerdictState] | None = None,
    coverage: dict[str, float] | None = None,
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

    if all_claims is not None:
        sentences.extend(build_gaps_sentences(sub_questions, all_claims, verdicts or [], coverage or {}, order))

    # Draft sentences are NOT persisted here: the Report Auditor writes
    # each sentence's final row exactly once, after its verdict. Persisting
    # drafts too produced two rows per sentence (draft + audited) and made
    # the trail's "failed audit" count look ~6x worse than reality.
    return sentences
