"""Phase 2 DoD (BLUEPRINT.md §17): a real question produces a report with
stored sources/passages/claims, running the full compiled LangGraph
(Planner -> Researcher -> Curator -> Drafter -> Synthesizer) end to end
against a mocked Mesh and a fake search provider — no real API key or
network access needed."""

from __future__ import annotations

import json

import httpx
import respx

from app.deps import Deps
from app.graph import build_graph, initial_state
from core.config import Settings
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient
from retrieval.base import ScrapedPage, SearchProvider, SearchResult
from store.models import Claim, Passage, Source, SubQuestion
from store.repo import Repo


class _FakeSearchProvider(SearchProvider):
    name = "fake"

    async def search(self, query: str, limit: int = 5):
        return [SearchResult(url="https://example.com/battery-report", title="Battery Report")]

    async def scrape(self, url: str):
        text = (
            "The global EV battery market reached $50 billion in 2025. "
            "Supply chain risk is concentrated in lithium and cobalt sourcing. "
        ) * 10
        return ScrapedPage(url=url, title="Battery Report", text=text, published_at=None)


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
                        "time_scope": "2025",
                        "premise_flag": False,
                    }
                ]
            }
        )
    elif "QueryPlan" in system_content:
        content = json.dumps({"queries": ["EV battery market size 2025"]})
    elif "DrafterOutput" in system_content:
        content = json.dumps(
            {"claims": [{"text": "The EV battery market reached $50 billion in 2025.", "passage_index": 0}]}
        )
    elif "SynthesizerOutput" in system_content:
        content = json.dumps(
            {
                "sections": [
                    {
                        "section": "Market size",
                        "sentences": [
                            {"text": "The EV battery market reached $50 billion in 2025.", "claim_indices": [0]}
                        ],
                    }
                ]
            }
        )
    elif "SpanJudgment" in system_content:
        content = json.dumps(
            {
                "evidence_span": "The global EV battery market reached $50 billion in 2025.",
                "entailment": "supported",
                "reason": "The passage directly states this figure.",
            }
        )
    elif "CoverageJudgment" in system_content:
        content = json.dumps({"criteria_satisfied": [True], "missing_aspect": None})
    else:
        content = "{}"

    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


@respx.mock
async def test_happy_path_produces_report_with_stored_evidence(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_mesh_side_effect)

    db_path = tmp_path / "test.db"
    repo = Repo(f"sqlite:///{db_path}")
    repo.create_all()

    mesh = MeshClient(
        api_key="rsk_test",
        cache=DiskCache(str(tmp_path / "cache"), enabled=False),
        breaker=CircuitBreaker(),
        max_concurrency=2,
    )
    settings = Settings(
        mesh_api_key="rsk_test",
        database_url=f"sqlite:///{db_path}",
        max_passages_per_call=12,
        max_passages_per_subquestion=30,
    )

    deps = Deps(mesh=mesh, search=_FakeSearchProvider(), repo=repo, settings=settings)

    run = repo.create_run(question="What is the size of the EV battery market?")
    graph = build_graph(deps)
    result = await graph.ainvoke(initial_state(run.id, run.question))

    assert len(result["sub_questions"]) >= 1
    assert len(result["sources"]) >= 1
    assert len(result["passages"]) >= 1
    assert len(result["claims"]) >= 1
    assert len(result["report_sentences"]) >= 1
    assert all(s["claim_ids"] for s in result["report_sentences"])  # no sentence rendered without a real citation
    assert result["stop_reason"] == "coverage_met"  # the verifier passed the claim and coverage was judged full
    assert any(c["status"] == "verified" for c in result["claims"])

    with repo.session() as session:
        from store.models import Verdict
        assert session.query(SubQuestion).filter_by(run_id=run.id).count() >= 1
        assert session.query(Source).filter_by(run_id=run.id).count() >= 1
        assert session.query(Passage).count() >= 1
        assert session.query(Claim).filter_by(run_id=run.id).count() >= 1
        assert session.query(Verdict).count() >= 1
