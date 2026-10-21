"""Resilience property (BLUEPRINT.md §10): one sub-question whose scrape
fails entirely must not prevent other sub-questions' evidence from being
collected."""

from __future__ import annotations

import json

import httpx
import respx

from agents.researcher import research_all
from app.deps import Deps
from app.state import SubQuestionState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient
from retrieval.base import ScrapedPage, SearchProvider, SearchResult


class _FakeSettings:
    mesh_max_concurrency = 2


class _FakeRepo:
    def __init__(self) -> None:
        self.sources: list = []
        self.searches: list = []

    def create_sources(self, run_id: str, sources: list) -> None:
        self.sources.extend(sources)

    def record_search(self, run_id, subq_id, query, provider, result_count, iteration=0) -> None:
        self.searches.append((subq_id, query, provider, result_count))


class _FlakyProvider(SearchProvider):
    name = "fake"

    async def search(self, query: str, limit: int = 5):
        return [SearchResult(url=f"https://example.com/{query}", title="t")]

    async def scrape(self, url: str):
        if "bad-question" in url:
            raise RuntimeError("scrape failed")
        return ScrapedPage(url=url, title="Example", text="Battery supply chains face raw material shortages. " * 20)


def _client(tmp_path):
    return MeshClient(
        api_key="rsk_test",
        cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(),
        max_concurrency=2,
    )


def _mesh_side_effect(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    user_content = body["messages"][1]["content"]
    query = "bad-question" if "bad-question" in user_content else "good-question"
    content = json.dumps({"queries": [query]})
    return httpx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@respx.mock
async def test_one_failing_subquestion_does_not_block_others(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_mesh_side_effect)

    subqs = [
        SubQuestionState(id="ok", text="good question", acceptance_criteria=["a"], time_scope=None, premise_flag=False),
        SubQuestionState(id="bad", text="bad-question", acceptance_criteria=["a"], time_scope=None, premise_flag=False),
    ]
    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=_FlakyProvider(), repo=repo, settings=_FakeSettings())

    sources = await research_all(deps, "run-1", subqs)

    assert any(s["subq_id"] == "ok" for s in sources)
    assert not any(s["subq_id"] == "bad" for s in sources)
    assert repo.sources == sources
