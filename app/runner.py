"""Shared run-execution logic for the CLI and the API (BLUEPRINT.md §10,
§12 "Replay mode"): one place that knows how to start a fresh run or
resume an interrupted one.

Resume uses LangGraph's own SQLite checkpointer, not the hand-rolled
`last_completed_node` trail field: the checkpointer snapshots the full
typed state after every node, so resuming re-enters exactly where a run
left off — no re-running completed nodes, no losing accumulated sources/
claims/verdicts. Verified against this installed LangGraph version in a
standalone smoke test before being relied on here.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents import auditor, contradiction, synthesizer
from app.deps import Deps
from app.graph import build_graph, initial_state
from app.state import VERIFIED_STATUSES, ClaimState, PassageState, SubQuestionState
from core.config import Settings
from core.logging import get_logger
from llm.client import MeshClient
from retrieval.firecrawl_client import FirecrawlProvider
from retrieval.null_provider import NullSearchProvider
from store.repo import Repo

logger = get_logger(__name__)


def build_deps(settings: Settings, repo: Repo) -> Deps:
    mesh = MeshClient.from_settings(settings, repo=repo)
    if settings.firecrawl_api_key:
        search = FirecrawlProvider(api_key=settings.firecrawl_api_key)
    else:
        logger.warning("no FIRECRAWL_API_KEY configured — running with NullSearchProvider (no real evidence)")
        search = NullSearchProvider()
    return Deps(mesh=mesh, search=search, repo=repo, settings=settings)


@contextlib.asynccontextmanager
async def _checkpointer(settings: Settings) -> AsyncIterator[AsyncSqliteSaver]:
    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path) as saver:
        yield saver


async def execute_run(settings: Settings, repo: Repo, run_id: str, question: str) -> dict:
    """Runs a FRESH graph for an already-created Run row. Used directly by
    `start_run` (CLI) and as the background task behind `POST /research`,
    which creates the Run itself (to return `run_id` immediately) and
    must not have a second Run row created underneath it."""
    deps = build_deps(settings, repo)
    repo.update_run_status(run_id, "in_progress")

    async with _checkpointer(settings) as checkpointer:
        graph = build_graph(deps, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        try:
            result = await graph.ainvoke(initial_state(run_id, question), config=config)
        except Exception:
            logger.error("run failed", extra={"extra_fields": {"run_id": run_id}}, exc_info=True)
            repo.update_run_status(run_id, "failed")
            raise
        repo.finalize_run(run_id, "complete", stop_reason=result.get("stop_reason"), iterations=result.get("iteration"))
        return result


async def start_run(settings: Settings, repo: Repo, question: str) -> str:
    """CLI convenience: create a Run and execute it immediately."""
    deps = build_deps(settings, repo)
    run = repo.create_run(question=question, config_snapshot={"search_provider": deps.search.name})
    await execute_run(settings, repo, run.id, question)
    return run.id


async def resume_run(settings: Settings, repo: Repo, run_id: str) -> None:
    """Re-enters an existing Run's graph from its last checkpoint. Used
    both for an explicit `cli.py resume <run_id>` and for the API's
    startup scan of runs left `in_progress` by a prior crash."""
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"no such run: {run_id}")

    deps = build_deps(settings, repo)
    repo.update_run_status(run_id, "in_progress")

    async with _checkpointer(settings) as checkpointer:
        graph = build_graph(deps, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        try:
            result = await graph.ainvoke(None, config=config)
        except Exception:
            logger.error("resumed run failed", extra={"extra_fields": {"run_id": run_id}}, exc_info=True)
            repo.update_run_status(run_id, "failed")
            raise
        repo.finalize_run(run_id, "complete", stop_reason=result.get("stop_reason"), iterations=result.get("iteration"))


async def resume_all_in_progress(settings: Settings, repo: Repo) -> list[str]:
    """Called on API startup (BLUEPRINT.md §10): finds every run a prior
    process left `in_progress` and resumes each from its last checkpoint.
    Returns the run_ids it attempted."""
    stale_runs = repo.find_in_progress_runs()
    resumed = []
    for run in stale_runs:
        resumed.append(run.id)
        try:
            await resume_run(settings, repo, run.id)
        except Exception:
            logger.error("startup resume failed for a run", extra={"extra_fields": {"run_id": run.id}}, exc_info=True)
    return resumed


async def resynthesize_run(settings: Settings, repo: Repo, run_id: str) -> dict[str, Any]:
    """Human-in-the-loop override (BLUEPRINT.md §4.1a): re-runs
    Contradiction Checker -> Synthesizer -> Report Auditor using CURRENT
    claim statuses, after a human has promoted or confirmed one via
    `Repo.override_claim_status`. Does NOT re-run retrieval, drafting, or
    verification — the AI's original Verdict rows are untouched; only the
    downstream stages that depend on claim status are redone, since a
    promoted claim can now appear in the report and can newly contradict
    an already-verified one.

    Spends real Mesh calls (contradiction + synthesis + audit roles) —
    this is an explicit, human-triggered action, not part of the
    automatic pipeline."""
    export = repo.export_run(run_id)
    deps = build_deps(settings, repo)

    sub_questions = [
        SubQuestionState(
            id=sq["id"], text=sq["text"], acceptance_criteria=sq["acceptance_criteria"],
            time_scope=sq["time_scope"], premise_flag=sq["premise_flag"],
        )
        for sq in export["sub_questions"]
    ]
    claims = [ClaimState(id=c["id"], subq_id=c["subq_id"], text=c["text"], passage_id=c["passage_id"], status=c["status"]) for c in export["claims"]]
    passages = [
        PassageState(id=p["id"], source_id=p["source_id"], subq_id=p["subq_id"], start=p["start"], end=p["end"], text=p["text"])
        for p in export["passages"]
    ]
    verified_claims = [c for c in claims if c["status"] in VERIFIED_STATUSES]

    repo.clear_contradictions(run_id)
    contradictions = await contradiction.check_all(deps, run_id, sub_questions, claims)

    draft_sentences = await synthesizer.synthesize(deps, run_id, sub_questions, verified_claims, contradictions)

    repo.clear_report_sentences(run_id)
    final_sentences = await auditor.audit_all(deps, run_id, draft_sentences, claims, passages)

    return {"report_sentences": final_sentences, "contradictions": contradictions}
