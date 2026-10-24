"""Claim Verifier (BLUEPRINT.md §4.1, §5 — "the core of this track").

Per claim: a cheap deterministic pre-filter, an LLM span-quote +
entailment judgment, a code-level check that the quoted span actually
exists in the passage, a code-level numeric-consistency check,
re-attribution against sibling passages on failure, and a code-level
confidence score. The model can lie about entailment; it's much harder
for it to fabricate a verbatim span that also fuzzy-matches real text it
never saw (DESIGN.md §1) — that's the check doing the real work here.

Must use a different model than the Report Auditor (Phase 4) — this
agent always resolves the `verifier` role from llm/models.yaml, never
`auditor` (DESIGN.md §4).
"""

from __future__ import annotations

import asyncio

from agents.schemas import SpanJudgment
from app.deps import Deps
from core.guardrails import wrap_untrusted
from app.state import ClaimState, PassageState, SourceState, VerdictState
from core.logging import get_logger
from core.relevance import passes_prefilter, score as relevance_score
from retrieval.dates import parse_published_date
from verify.confidence import score_confidence
from verify.numeric_check import numbers_consistent
from verify.gap_reason import reason_admits_gap
from verify.reattribute import candidate_passages
from verify.scope_check import scope_consistent
from verify.span_match import span_exists

logger = get_logger(__name__)

PROMPT_VERSION = "verifier.v1"


def _make_verdict(
    claim: ClaimState,
    stage_reached: str,
    stage_failed: str | None,
    outcome: str,
    judgment: SpanJudgment | None,
    passage_id: str | None,
    reason: str | None,
    confidence: tuple[str, float] | None,
    called_mesh: bool,
) -> VerdictState:
    return VerdictState(
        claim_id=claim["id"],
        stage_reached=stage_reached,
        stage_failed=stage_failed,
        outcome=outcome,
        entailment=judgment.entailment if judgment else None,
        evidence_span=judgment.evidence_span if judgment else None,
        reason=reason,
        passage_id=passage_id,
        model="verifier" if called_mesh else "n/a",
        prompt_version=PROMPT_VERSION if called_mesh else "n/a",
        confidence_level=confidence[0] if confidence else None,
        confidence_score=confidence[1] if confidence else None,
    )


CORROBORATION_MIN_SIMILARITY = 0.5


def _confidence_for(
    entailment: str,
    passage: PassageState,
    source_by_id: dict[str, SourceState],
    time_scope: str | None = None,
    corroborating_sources: int = 1,
) -> tuple[str, float]:
    source = source_by_id.get(passage["source_id"])
    tier = source.get("tier", 4) if source else 4
    raw_published = source.get("published_at") if source else None
    published_at = parse_published_date(raw_published) if raw_published else None
    return score_confidence(
        entailment=entailment, tier=tier, corroborating_sources=corroborating_sources,
        published_at=published_at, time_scope=time_scope,
    )


async def _judge(deps: Deps, run_id: str, claim_text: str, passage_text: str) -> SpanJudgment | None:
    try:
        return await deps.mesh.complete_json(
            node="verifier",
            prompt_name="verifier.v1",
            variables={"claim": claim_text, "passage": wrap_untrusted(passage_text)},
            schema=SpanJudgment,
            model_role="verifier",
            run_id=run_id,
        )
    except Exception:
        logger.warning("verifier span-quote call failed", exc_info=True)
        return None


def _passes_stage2_and_4(judgment: SpanJudgment, passage_text: str, claim_text: str) -> tuple[bool, bool]:
    span_ok = span_exists(judgment.evidence_span, passage_text)
    numbers_ok, _missing = numbers_consistent(claim_text, judgment.evidence_span)
    # Stage 4b: scope-widening words must be present in the passage (verify/scope_check.py).
    # Folded into `numbers_ok` so every caller treats it as the same "consistency" gate.
    scope_ok, _missing_scope = scope_consistent(claim_text, passage_text)
    return span_ok, numbers_ok and scope_ok


async def _verify_one(
    deps: Deps,
    run_id: str,
    claim: ClaimState,
    passage_by_id: dict[str, PassageState],
    passages_for_subq: list[PassageState],
    source_by_id: dict[str, SourceState],
    time_scopes: dict[str, str | None],
) -> tuple[ClaimState, VerdictState]:
    time_scope = time_scopes.get(claim["subq_id"])
    passage = passage_by_id.get(claim["passage_id"])
    if passage is None:
        verdict = _make_verdict(claim, "prefilter", "missing_passage", "unverified", None, None, "cited passage no longer exists", None, False)
        return {**claim, "status": "unverified"}, verdict

    # Stage 0: cheap deterministic pre-filter — no meaningful lexical
    # overlap at all means this is worth rejecting without spending a
    # Mesh call to find out the hard way (BLUEPRINT.md §5 stage 0, §11).
    if not passes_prefilter(claim["text"], passage["text"], threshold=deps.settings.relevance_prefilter_threshold):
        verdict = _make_verdict(claim, "prefilter", "no_lexical_overlap", "rejected", None, passage["id"], "claim and cited passage share no meaningful overlap", None, False)
        return {**claim, "status": "rejected"}, verdict

    judgment = await _judge(deps, run_id, claim["text"], passage["text"])
    if judgment is None:
        verdict = _make_verdict(claim, "span_quote", "llm_call_failed", "unverified", None, passage["id"], "verifier call failed", None, True)
        return {**claim, "status": "unverified"}, verdict

    span_ok, numbers_ok = _passes_stage2_and_4(judgment, passage["text"], claim["text"])
    passed = span_ok and numbers_ok and judgment.entailment in ("supported", "partial")
    self_reported_gap = reason_admits_gap(judgment.entailment, judgment.reason)  # verify/gap_reason.py
    passed = passed and not self_reported_gap

    if passed:
        outcome = "verified" if judgment.entailment == "supported" else "partial"
        confidence = _confidence_for(judgment.entailment, passage, source_by_id, time_scope)
        verdict = _make_verdict(claim, "confidence", None, outcome, judgment, passage["id"], judgment.reason, confidence, True)
        return {**claim, "status": outcome}, verdict

    if judgment.entailment in ("unsupported", "contradicted"):
        stage_failed = "entailment"
    elif not span_ok:
        stage_failed = "span_missing"
    elif self_reported_gap:
        stage_failed = "self_reported_gap"
    elif not scope_consistent(claim["text"], passage["text"])[0]:
        stage_failed = "scope_widening"
    else:
        stage_failed = "numeric_mismatch"

    # Stage 5: re-attribution — try sibling passages for this sub-question
    # before giving up on an otherwise-plausible claim (DESIGN.md §5).
    for candidate in candidate_passages(claim["text"], passages_for_subq, exclude_id=passage["id"], k=5):
        cand_judgment = await _judge(deps, run_id, claim["text"], candidate["text"])
        if cand_judgment is None:
            continue
        cand_span_ok, cand_numbers_ok = _passes_stage2_and_4(cand_judgment, candidate["text"], claim["text"])
        if (
            cand_span_ok and cand_numbers_ok and cand_judgment.entailment in ("supported", "partial")
            and not reason_admits_gap(cand_judgment.entailment, cand_judgment.reason)
        ):
            confidence = _confidence_for(cand_judgment.entailment, candidate, source_by_id, time_scope)
            verdict = _make_verdict(claim, "confidence", None, "re_cited", cand_judgment, candidate["id"], cand_judgment.reason, confidence, True)
            return {**claim, "status": "re_cited", "passage_id": candidate["id"]}, verdict

    final_outcome = "unverified" if judgment.entailment == "partial" else "rejected"
    verdict = _make_verdict(claim, "reattribution", stage_failed, final_outcome, judgment, passage["id"], judgment.reason, None, True)
    return {**claim, "status": final_outcome}, verdict


def _apply_corroboration(
    claims: list[ClaimState],
    verdicts: list[VerdictState],
    passage_by_id: dict[str, PassageState],
    source_by_id: dict[str, SourceState],
    time_scopes: dict[str, str | None],
) -> None:
    """BLUEPRINT.md §5 stage 6: confidence depends on how many INDEPENDENT
    sources support the same fact. Runs after the whole batch is verified
    (a claim's peers don't exist yet while it's being judged): for each
    newly passed verdict, count distinct other sources, within the same
    sub-question, that have a passed claim lexically near-identical to it
    (core/relevance.py, same scorer as everywhere else — no LLM call),
    then recompute its confidence. Mutates `verdicts` in place."""
    claim_by_id = {c["id"]: c for c in claims}
    passed = [c for c in claims if c["status"] in ("verified", "partial", "re_cited")]
    for verdict in verdicts:
        claim = claim_by_id.get(verdict["claim_id"])
        passage = passage_by_id.get(verdict["passage_id"]) if verdict["passage_id"] else None
        if claim is None or passage is None or verdict["confidence_score"] is None:
            continue
        own_source = passage["source_id"]
        peer_sources: set[str] = set()
        for other in passed:
            if other["id"] == claim["id"] or other["subq_id"] != claim["subq_id"]:
                continue
            other_passage = passage_by_id.get(other["passage_id"])
            if other_passage is None or other_passage["source_id"] == own_source:
                continue
            if relevance_score(claim["text"], other["text"]) >= CORROBORATION_MIN_SIMILARITY:
                peer_sources.add(other_passage["source_id"])
        if peer_sources:
            level, numeric = _confidence_for(
                verdict["entailment"] or "supported", passage, source_by_id,
                time_scopes.get(claim["subq_id"]), corroborating_sources=1 + len(peer_sources),
            )
            verdict["confidence_level"], verdict["confidence_score"] = level, numeric


async def verify_all(
    deps: Deps,
    run_id: str,
    claims: list[ClaimState],
    passages: list[PassageState],
    sources: list[SourceState],
    already_verified_ids: set[str],
    time_scopes: dict[str, str | None] | None = None,
) -> tuple[list[ClaimState], list[VerdictState]]:
    time_scopes = time_scopes or {}
    passage_by_id = {p["id"]: p for p in passages}
    source_by_id = {s["id"]: s for s in sources}
    passages_by_subq: dict[str, list[PassageState]] = {}
    for p in passages:
        passages_by_subq.setdefault(p["subq_id"], []).append(p)

    to_verify = [c for c in claims if c["id"] not in already_verified_ids]

    # Verified concurrently, not one at a time (DESIGN.md §16): each
    # claim's Mesh calls are independent, and MeshClient's own semaphore
    # (BLUEPRINT.md §11) already bounds real network concurrency — this
    # is a pure latency win, same calls and same cost, just not waiting
    # for each claim to finish before starting the next. The Verifier is
    # by far the highest-volume role (297/422 calls on the first live
    # run), so this is where parallelizing matters most.
    results = await asyncio.gather(
        *(
            _verify_one(deps, run_id, claim, passage_by_id, passages_by_subq.get(claim["subq_id"], []), source_by_id, time_scopes)
            for claim in to_verify
        )
    )
    updated_claims: list[ClaimState] = [r[0] for r in results]
    verdicts: list[VerdictState] = [r[1] for r in results]

    updated_by_id = {c["id"]: c for c in updated_claims}
    merged = [updated_by_id.get(c["id"], c) for c in claims]
    _apply_corroboration(merged, verdicts, passage_by_id, source_by_id, time_scopes)

    if verdicts:
        deps.repo.create_verdicts(verdicts)
        deps.repo.update_claim_statuses(updated_claims)

    return merged, verdicts
