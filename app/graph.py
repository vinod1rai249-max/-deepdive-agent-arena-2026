"""LangGraph wiring (BLUEPRINT.md §4):

  Planner -> Researcher -> Curator -> Drafter -> Verifier -> loop_decision
                 ^                                                |
                 |________ (thin sub-questions, budget OK) ________|
                                                                    |
                                                    (otherwise) -> Synthesizer

Curator/Drafter/Verifier each follow the same "append-only, skip
already-processed" pattern (BLUEPRINT.md §12): every node only processes
whatever hasn't already produced downstream artifacts, so a re-research
loop never regenerates or orphans work from an earlier iteration. Only
the Researcher and the loop-decision node need to know which specific
sub-questions are "thin" this round.

Each node calls `deps.repo.mark_run_node` only after its own agent module
has already persisted its output — this is a human-readable trail marker
("how far did this run get"), not the resume mechanism itself. True
resume-after-crash (BLUEPRINT.md §10) uses LangGraph's own checkpointer
(`build_graph(deps, checkpointer=...)`, see app/runner.py): it snapshots
the full typed state after every node, so `ainvoke(None, config={...,
"thread_id": run_id})` continues from exactly where a run left off,
without re-running completed nodes or losing any accumulated state.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents import auditor, contradiction, curator, drafter, loop_control, planner, researcher, synthesizer, verifier
from app.deps import Deps
from app.state import VERIFIED_STATUSES, RunState
from core.logging import bind


def build_graph(deps: Deps, checkpointer=None) -> CompiledStateGraph:
    graph = StateGraph(RunState)

    async def planner_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="planner")
        sub_questions = await planner.plan(deps, state["run_id"], state["question"])
        deps.repo.mark_run_node(state["run_id"], "planner")
        return {"sub_questions": sub_questions}

    async def researcher_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="researcher")
        target_ids = set(state["research_targets"]) if state["research_targets"] else {
            sq["id"] for sq in state["sub_questions"]
        }
        target_subqs = [sq for sq in state["sub_questions"] if sq["id"] in target_ids]

        new_sources = await researcher.research_all(deps, state["run_id"], target_subqs, state["failure_reasons"])
        deps.repo.mark_run_node(state["run_id"], "researcher")

        seen = set(state["seen_content_hashes"])
        genuinely_new = [s for s in new_sources if s["content_hash"] not in seen]
        no_new_evidence = bool(state["research_targets"]) and not genuinely_new

        return {
            "sources": state["sources"] + new_sources,
            "seen_content_hashes": list(seen | {s["content_hash"] for s in new_sources}),
            "no_new_evidence_triggered": no_new_evidence,
        }

    def curator_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="curator")
        already_curated_source_ids = {p["source_id"] for p in state["passages"]}
        new_sources = [s for s in state["sources"] if s["id"] not in already_curated_source_ids]
        deps.repo.mark_run_node(state["run_id"], "curator")
        if not new_sources:
            return {}
        new_passages = curator.curate(
            deps, state["run_id"], state["sub_questions"], new_sources, existing_passages=state["passages"]
        )
        return {"passages": state["passages"] + new_passages}

    async def drafter_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="drafter")
        already_drafted_passage_ids = {c["passage_id"] for c in state["claims"]}
        new_passages = [p for p in state["passages"] if p["id"] not in already_drafted_passage_ids]
        deps.repo.mark_run_node(state["run_id"], "drafter")
        if not new_passages:
            return {}
        new_claims = await drafter.draft_all(deps, state["run_id"], state["sub_questions"], new_passages)
        return {"claims": state["claims"] + new_claims}

    async def verifier_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="verifier")
        already_verified_ids = {v["claim_id"] for v in state["verdicts"]}
        updated_claims, new_verdicts = await verifier.verify_all(
            deps, state["run_id"], state["claims"], state["passages"], state["sources"], already_verified_ids
        )
        deps.repo.mark_run_node(state["run_id"], "verifier")
        return {"claims": updated_claims, "verdicts": state["verdicts"] + new_verdicts}

    async def loop_decision_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="loop_decision")
        update = await loop_control.decide(deps, state["run_id"], state)
        deps.repo.mark_run_node(state["run_id"], "loop_decision")
        return update

    async def contradiction_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="contradiction")
        contradictions = await contradiction.check_all(deps, state["run_id"], state["sub_questions"], state["claims"])
        deps.repo.mark_run_node(state["run_id"], "contradiction")
        return {"contradictions": contradictions}

    async def synthesizer_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="synthesizer")
        verified_claims = [c for c in state["claims"] if c["status"] in VERIFIED_STATUSES]
        sentences = await synthesizer.synthesize(
            deps, state["run_id"], state["sub_questions"], verified_claims, state["contradictions"]
        )
        deps.repo.mark_run_node(state["run_id"], "synthesizer")
        return {"report_sentences": sentences}

    async def auditor_node(state: RunState) -> dict:
        bind(run_id=state["run_id"], node="auditor")
        final_sentences = await auditor.audit_all(
            deps, state["run_id"], state["report_sentences"], state["claims"], state["passages"]
        )
        deps.repo.mark_run_node(state["run_id"], "auditor")
        return {"report_sentences": final_sentences}

    def route_after_loop_decision(state: RunState) -> str:
        return "contradiction" if state.get("stop_reason") else "researcher"

    graph.add_node("planner", planner_node)
    graph.add_node("researcher", researcher_node)
    graph.add_node("curator", curator_node)
    graph.add_node("drafter", drafter_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("loop_decision", loop_decision_node)
    graph.add_node("contradiction", contradiction_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("auditor", auditor_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("researcher", "curator")
    graph.add_edge("curator", "drafter")
    graph.add_edge("drafter", "verifier")
    graph.add_edge("verifier", "loop_decision")
    graph.add_conditional_edges(
        "loop_decision", route_after_loop_decision, {"researcher": "researcher", "contradiction": "contradiction"}
    )
    graph.add_edge("contradiction", "synthesizer")
    graph.add_edge("synthesizer", "auditor")
    graph.add_edge("auditor", END)

    return graph.compile(checkpointer=checkpointer)


def initial_state(run_id: str, question: str, max_iterations: int = 3) -> RunState:
    import time

    return RunState(
        run_id=run_id,
        question=question,
        sub_questions=[],
        sources=[],
        passages=[],
        claims=[],
        verdicts=[],
        contradictions=[],
        report_sentences=[],
        started_at=time.monotonic(),
        iteration=0,
        max_iterations=max_iterations,
        coverage={},
        stop_reason=None,
        research_targets=[],
        failure_reasons={},
        seen_content_hashes=[],
        no_new_evidence_triggered=False,
    )
