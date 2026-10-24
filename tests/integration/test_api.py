"""Phase 5: exercises the real FastAPI endpoints end to end (request
routing, background execution, response shapes, 404s) against a mocked
Mesh and the graceful-degradation NullSearchProvider — no real network
access needed. Lifespan's own resume-scan is exercised separately by
tests/unit/test_runner.py; here `app.state` is populated directly the
same way lifespan would, so the endpoints themselves are what's under
test, not the ASGI lifespan protocol."""

from __future__ import annotations

import json

import httpx
import respx

from app.api import app
from core.config import Settings
from store.repo import Repo


def _mesh_side_effect(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    system_content = body["messages"][0]["content"]

    if "PlannerOutput" in system_content:
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


@respx.mock
async def test_research_lifecycle_through_the_api(tmp_path):
    """No Firecrawl key configured -> NullSearchProvider -> the run
    completes via graceful degradation (BLUEPRINT.md §10) with
    stop_reason="no_new_evidence" and an "insufficient evidence" report,
    exercising /research, /runs/{id}, and /runs/{id}/trail for real."""
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_mesh_side_effect)

    repo = Repo(f"sqlite:///{tmp_path / 'test.db'}")
    repo.create_all()
    settings = Settings(
        mesh_api_key="rsk_test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        checkpoint_db_path=str(tmp_path / "checkpoints.db"),
        firecrawl_api_key=None, search_provider="null",
        deadline_seconds_per_run=600,
        llm_cache_dir=str(tmp_path / "llm_cache"),  # isolated — never share the real ./.cache/llm across tests
    )
    app.state.repo = repo
    app.state.settings = settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200

        resp = await client.post("/research", json={"question": "What is the size of the EV battery market?"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        missing = await client.get("/runs/does-not-exist")
        assert missing.status_code == 404

        # Poll until the background task finishes (bounded wait).
        import asyncio
        status = None
        for _ in range(50):
            run_resp = await client.get(f"/runs/{run_id}")
            status = run_resp.json()["status"]
            if status in ("complete", "failed"):
                break
            await asyncio.sleep(0.1)

        assert status == "complete"
        assert run_resp.json()["stop_reason"] == "no_new_evidence"
        assert run_resp.json()["report"]  # the "insufficient evidence" placeholder is still a report

        trail = await client.get(f"/runs/{run_id}/trail")
        assert trail.status_code == 200
        assert trail.json()["sub_questions"]
        assert trail.json()["human_reviews"] == []


def _seed_claim_for_override(repo: Repo, run_id: str) -> None:
    from store.models import Claim, Passage, Run, Source, SubQuestion

    with repo.session() as session:
        session.add(Run(id=run_id, question="What happened with the merger?"))
        session.add(
            SubQuestion(id="subq-1", run_id=run_id, text="q", acceptance_criteria=["a"], time_scope=None, premise_flag=False)
        )
        session.add(
            Source(
                id="source-1", run_id=run_id, subq_id="subq-1", url="https://example.com/x", domain="example.com",
                title=None, tier=3, content_hash="h1", text="The merger was finalized.", origin="web",
            )
        )
        session.add(Passage(id="passage-1", source_id="source-1", start=0, end=10, text="The merger was finalized."))
        session.add(
            Claim(id="claim-1", run_id=run_id, subq_id="subq-1", text="The merger was finalized.", passage_id="passage-1", status="rejected")
        )


def _override_mesh_side_effect(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    system_content = body["messages"][0]["content"]
    if "SynthesizerOutput" in system_content:
        content = json.dumps(
            {"sections": [{"section": "Merger", "sentences": [{"text": "The merger was finalized.", "claim_indices": [0]}]}]}
        )
    elif "SpanJudgment" in system_content:
        content = json.dumps({"evidence_span": "The merger was finalized.", "entailment": "supported", "reason": "r"})
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


@respx.mock
async def test_override_endpoint_promotes_a_claim_and_resynthesizes(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_override_mesh_side_effect)

    repo = Repo(f"sqlite:///{tmp_path / 'test2.db'}")
    repo.create_all()
    _seed_claim_for_override(repo, "run-override")
    settings = Settings(
        mesh_api_key="rsk_test",
        database_url=f"sqlite:///{tmp_path / 'test2.db'}",
        checkpoint_db_path=str(tmp_path / "checkpoints2.db"),
        firecrawl_api_key=None, search_provider="null",
        llm_cache_dir=str(tmp_path / "llm_cache2"),
    )
    app.state.repo = repo
    app.state.settings = settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_claim = await client.post(
            "/runs/run-override/claims/no-such-claim/override",
            json={"new_status": "verified", "reason": "x"},
        )
        assert missing_claim.status_code == 404

        resp = await client.post(
            "/runs/run-override/claims/claim-1/override",
            json={"new_status": "verified", "reason": "Confirmed accurate against the source directly."},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["new_status"] == "verified"
        # 1 cited sentence + 1 deterministic "Gaps / Unverified" note (the
        # seeded sub-question has 0 coverage), so assert on cited ones.
        assert body["report_sentence_count"] == 2

        report = await client.get("/runs/run-override")
        cited = [s for s in report.json()["report"] if s["citations"]]
        assert len(cited) == 1
        assert any(s["section"] == "Gaps / Unverified" for s in report.json()["report"])

        trail = await client.get("/runs/run-override/trail")
        assert len(trail.json()["human_reviews"]) == 1
