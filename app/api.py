"""FastAPI app (BLUEPRINT.md §13). `/research` kicks off a run in the
background; `/runs/{id}` and `/runs/{id}/trail` are the auditable record
(BLUEPRINT.md §2.5) a judge or the UI reads from — the report's citations
resolve to real source titles/URLs/tiers, never an internal-only ID
(DESIGN.md §3). On startup, any run a prior crash left `in_progress` is
resumed from its last checkpoint (BLUEPRINT.md §10), not restarted.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.followup import answer_followup
from app.runner import build_deps, execute_run, resume_all_in_progress, resynthesize_run
from core.config import get_settings
from core.logging import configure_logging, get_logger
from store.repo import Repo

logger = get_logger(__name__)

_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    repo = Repo(settings.database_url)
    repo.create_all()
    app.state.repo = repo
    app.state.settings = settings

    resumed = await resume_all_in_progress(settings, repo)
    if resumed:
        logger.warning("resumed runs left in_progress by a prior process", extra={"extra_fields": {"run_ids": resumed}})

    yield


app = FastAPI(title="DeepDive", lifespan=lifespan)


class ResearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class ResearchResponse(BaseModel):
    run_id: str


def _run_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/research", response_model=ResearchResponse)
async def create_research(req: ResearchRequest) -> ResearchResponse:
    repo: Repo = app.state.repo
    run = repo.create_run(question=req.question)
    _run_background(execute_run(app.state.settings, repo, run.id, req.question))
    return ResearchResponse(run_id=run.id)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    repo: Repo = app.state.repo
    trail = repo.get_trail(run_id)
    if trail["status"] is None:
        raise HTTPException(status_code=404, detail="run not found")
    report = repo.get_report(run_id)
    return {
        "run_id": run_id,
        "question": trail["question"],
        "status": trail["status"],
        "stop_reason": trail["stop_reason"],
        "iterations": trail["iterations"],
        "cost_usd": trail["llm_calls"]["total_cost_usd"],
        "report": report,
    }


@app.get("/runs/{run_id}/trail")
def get_trail(run_id: str) -> dict:
    repo: Repo = app.state.repo
    trail = repo.get_trail(run_id)
    if trail["status"] is None:
        raise HTTPException(status_code=404, detail="run not found")
    trail["human_reviews"] = repo.get_human_reviews(run_id)
    return trail


class ClaimOverrideRequest(BaseModel):
    """Human-in-the-loop override (BLUEPRINT.md §4.1a)."""

    new_status: Literal["verified", "partial", "rejected", "unverified"]
    reason: str = Field(min_length=1, max_length=500)


@app.post("/runs/{run_id}/claims/{claim_id}/override")
async def override_claim(run_id: str, claim_id: str, req: ClaimOverrideRequest) -> dict:
    """A human promotes a rejected/unverified/partial claim, or confirms
    the AI's verdict, with a required reason. Triggers a re-synthesis
    (Contradiction Checker -> Synthesizer -> Report Auditor only — not a
    full re-run) so the change is reflected in the report immediately."""
    repo: Repo = app.state.repo
    try:
        repo.override_claim_status(run_id, claim_id, req.new_status, req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    result = await resynthesize_run(app.state.settings, repo, run_id)
    return {
        "run_id": run_id,
        "claim_id": claim_id,
        "new_status": req.new_status,
        "report_sentence_count": len(result["report_sentences"]),
        "contradiction_count": len(result["contradictions"]),
    }


class FollowupRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@app.post("/runs/{run_id}/followup")
async def followup(run_id: str, req: FollowupRequest) -> dict:
    """Answers a new question from the evidence this run already retrieved
    (no new search); every sentence is re-verified before it is returned."""
    repo: Repo = app.state.repo
    if repo.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return await answer_followup(build_deps(app.state.settings, repo), run_id, req.question)


@app.get("/runs/{run_id}/report.md")
def get_report_markdown(run_id: str) -> str:
    repo: Repo = app.state.repo
    trail = repo.get_trail(run_id)
    if trail["status"] is None:
        raise HTTPException(status_code=404, detail="run not found")
    report = repo.get_report(run_id)

    lines = [f"# {trail['question']}", ""]
    for sentence in report:
        lines.append(f"- {sentence['text']}")
        for c in sentence["citations"]:
            lines.append(f"  - [{c['title'] or c['url']}]({c['url']}) (tier {c['tier']})")
    return "\n".join(lines)


@app.get("/runs/{run_id}/events")
async def stream_events(run_id: str) -> StreamingResponse:
    """Polling-based SSE (BLUEPRINT.md §13): yields the run's status and
    last completed node every second until it reaches a terminal state.
    Simpler than a true node-level push stream, and sufficient for a
    progress UI — the full trail is always available via /trail."""
    repo: Repo = app.state.repo

    async def event_stream():
        last_payload = None
        for _ in range(600):  # ~10 minutes of polling, matches the default run deadline
            run = repo.get_run(run_id)
            if run is None:
                yield "event: error\ndata: run not found\n\n"
                return
            payload = f"status={run.status} last_node={run.last_completed_node}"
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if run.status in ("complete", "failed"):
                return
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
