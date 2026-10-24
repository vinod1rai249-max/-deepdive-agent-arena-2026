"""Phase 3 DoD (BLUEPRINT.md §17): loop fires on a thin sub-question and
records stop_reason — exercised at the full-graph level, not just
loop_control in isolation. First pass finds weak, unsupported evidence
(claim rejected, coverage 0) which fires a real re-research pass; the
second pass finds supporting evidence and the run stops with
stop_reason="coverage_met"."""

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
from store.repo import Repo


class _TwoPassSearchProvider(SearchProvider):
    name = "fake"

    async def search(self, query: str, limit: int = 5):
        if "strong" in query:
            return [SearchResult(url="https://example.com/strong", title="Strong Source")]
        return [SearchResult(url="https://example.com/weak", title="Weak Source")]

    async def scrape(self, url: str):
        if "strong" in url:
            text = "The global EV battery market reached $50 billion in 2025. " * 10
            return ScrapedPage(url=url, title="Strong Source", text=text, published_at=None)
        text = "The company held an unrelated conference in a different city last year. " * 10
        return ScrapedPage(url=url, title="Weak Source", text=text, published_at=None)


def _mesh_side_effect(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    system_content = body["messages"][0]["content"]
    user_content = body["messages"][1]["content"]

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
        query = "strong market size query" if "all_rejected" in user_content else "weak query"
        content = json.dumps({"queries": [query]})
    elif "DrafterOutput" in system_content:
        if "conference" in user_content:
            content = json.dumps({"claims": [{"text": "The company expanded into new markets.", "passage_index": 0}]})
        else:
            content = json.dumps(
                {"claims": [{"text": "The EV battery market reached $50 billion in 2025.", "passage_index": 0}]}
            )
    elif "SpanJudgment" in system_content:
        if "conference" in user_content:
            content = json.dumps(
                {
                    "evidence_span": "The company held an unrelated conference in a different city last year.",
                    "entailment": "unsupported",
                    "reason": "This passage is about a conference, not market size.",
                }
            )
        else:
            content = json.dumps(
                {
                    "evidence_span": "The global EV battery market reached $50 billion in 2025.",
                    "entailment": "supported",
                    "reason": "The passage directly states this figure.",
                }
            )
    elif "CoverageJudgment" in system_content:
        content = json.dumps({"criteria_satisfied": [True], "missing_aspect": None})
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
async def test_thin_subquestion_triggers_real_re_research_then_succeeds(tmp_path):
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
        deadline_seconds_per_run=600,
    )

    deps = Deps(mesh=mesh, search=_TwoPassSearchProvider(), repo=repo, settings=settings)

    run = repo.create_run(question="What is the size of the EV battery market?")
    graph = build_graph(deps)
    result = await graph.ainvoke(initial_state(run.id, run.question))

    # The loop actually fired: two research passes happened (weak, then
    # targeted re-research), producing sources/claims from both.
    assert result["iteration"] == 1
    assert len(result["sources"]) == 2
    assert len(result["claims"]) == 2
    assert result["stop_reason"] == "coverage_met"

    statuses = {c["text"]: c["status"] for c in result["claims"]}
    assert statuses["The company expanded into new markets."] == "rejected"
    assert statuses["The EV battery market reached $50 billion in 2025."] == "verified"

    # Only the verified claim is asserted as a cited finding; the rejected
    # one appears only as an uncited note in "Gaps / Unverified".
    cited = [s for s in result["report_sentences"] if s["claim_ids"]]
    assert len(cited) == 1
    gaps = [s for s in result["report_sentences"] if s["section"] == "Gaps / Unverified"]
    assert any("The company expanded into new markets." in g["text"] for g in gaps)
    assert not any("The company expanded into new markets." in s["text"] for s in cited)
