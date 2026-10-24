"""Phase 5: resume-after-crash through the real app/runner.py + LangGraph
checkpointer (not a toy graph) — proves a run that crashes partway
through resumes from its last checkpoint rather than restarting, by
counting how many times the Planner's Mesh call actually fires.

Every agent-level Mesh call in this codebase degrades gracefully by
design (BLUEPRINT.md §10) — a plain LLM failure can't crash a run, it's
absorbed and recorded. So this simulates a lower-level failure a
try/except doesn't catch: a DB write error in the Researcher's
persistence call, right after the Planner node has already completed and
been checkpointed.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.runner import execute_run, resume_run
from core.config import Settings
from store.repo import Repo


def _mesh_side_effect(planner_calls: dict):
    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system_content = body["messages"][0]["content"]

        if "PlannerOutput" in system_content:
            planner_calls["n"] += 1
            content = json.dumps(
                {
                    "sub_questions": [
                        {
                            "text": "What is the size of the EV battery market?",
                            "acceptance_criteria": ["gives a market size figure"],
                            "time_scope": None,
                            "premise_flag": False,
                        }
                    ]
                }
            )
        elif "QueryPlan" in system_content:
            content = json.dumps({"queries": ["EV battery market size"]})
        else:
            content = "{}"

        return httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    return side_effect


@respx.mock
async def test_resume_does_not_rerun_the_completed_planner_node(tmp_path, monkeypatch):
    planner_calls = {"n": 0}
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_mesh_side_effect(planner_calls))

    db_path = tmp_path / "test.db"
    repo = Repo(f"sqlite:///{db_path}")
    repo.create_all()
    settings = Settings(
        mesh_api_key="rsk_test",
        database_url=f"sqlite:///{db_path}",
        checkpoint_db_path=str(tmp_path / "checkpoints.db"),
        firecrawl_api_key=None, search_provider="null",
        deadline_seconds_per_run=600,
        llm_cache_dir=str(tmp_path / "llm_cache"),  # isolated — never share the real ./.cache/llm across tests
    )

    run = repo.create_run(question="What is the size of the EV battery market?")

    # Simulate an infra failure (e.g. a DB write error) right as the
    # Researcher node finishes — something no agent-level try/except
    # catches — happening right after the Planner node has already
    # succeeded and been checkpointed.
    original_mark_run_node = Repo.mark_run_node
    call_state = {"should_fail": True}

    def flaky_mark_run_node(self, run_id, node):
        if call_state["should_fail"] and node == "researcher":
            raise RuntimeError("simulated database outage")
        return original_mark_run_node(self, run_id, node)

    monkeypatch.setattr(Repo, "mark_run_node", flaky_mark_run_node)

    try:
        await execute_run(settings, repo, run.id, run.question)
        raised = False
    except Exception:
        raised = True
    assert raised

    failed_run = repo.get_run(run.id)
    assert failed_run.status == "failed"
    assert planner_calls["n"] == 1

    # "Outage" is over; resume should pick up right after the already-
    # checkpointed Planner node and complete without re-calling it.
    call_state["should_fail"] = False
    await resume_run(settings, repo, run.id)

    final_run = repo.get_run(run.id)
    assert final_run.status == "complete"
    assert planner_calls["n"] == 1  # Planner was NOT re-run on resume
