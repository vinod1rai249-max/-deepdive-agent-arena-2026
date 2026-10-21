"""Tests the baseline scoring judge (BLUEPRINT.md §15): each of the
baseline's LLM-authored citations is actually fetched and graded by the
same standard the Report Auditor applies to DeepDive's own sentences —
this is what gives EVAL.md's baseline comparison a real number instead
of just unscored text."""

from __future__ import annotations

import json

import httpx
import respx

from app.deps import Deps
from eval.baseline import BaselineAnswer, BaselineSentence, score_baseline_answer
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient
from retrieval.base import ScrapedPage, SearchProvider


class _FakeProvider(SearchProvider):
    name = "fake"

    async def search(self, query: str, limit: int = 5):
        return []

    async def scrape(self, url: str):
        if "real-source" in url:
            return ScrapedPage(url=url, title="Real", text="The merger was finalized last month after regulatory approval.")
        return None  # simulates an unfetchable / nonexistent URL


def _client(tmp_path) -> MeshClient:
    return MeshClient(
        api_key="rsk_test", cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(), max_concurrency=2,
    )


@respx.mock
async def test_supported_and_unfetchable_citations_scored_correctly(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"evidence_span": "The merger was finalized last month after regulatory approval.", "entailment": "supported", "reason": "matches"}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )

    answer = BaselineAnswer(
        sentences=[
            BaselineSentence(text="The merger was finalized last month.", source_url="https://example.com/real-source"),
            BaselineSentence(text="The sky is green.", source_url="https://example.com/nonexistent-page"),
        ]
    )
    deps = Deps(mesh=_client(tmp_path), search=_FakeProvider(), repo=None, settings=None)

    result = await score_baseline_answer(deps, answer)

    assert result["n_sentences"] == 2
    assert result["citation_precision"] == 0.5
    assert result["details"][0]["supported"] is True
    assert result["details"][1]["supported"] is False
    assert result["details"][1]["url_fetchable"] is False


async def test_unsafe_url_is_scored_unsupported_without_a_mesh_call(tmp_path):
    answer = BaselineAnswer(sentences=[BaselineSentence(text="x", source_url="http://127.0.0.1/admin")])
    deps = Deps(mesh=_client(tmp_path), search=_FakeProvider(), repo=None, settings=None)

    result = await score_baseline_answer(deps, answer)

    assert result["citation_precision"] == 0.0
    assert result["details"][0]["url_fetchable"] is False


async def test_no_sentences_is_none_not_zero(tmp_path):
    answer = BaselineAnswer(sentences=[])
    deps = Deps(mesh=_client(tmp_path), search=_FakeProvider(), repo=None, settings=None)

    result = await score_baseline_answer(deps, answer)

    assert result["citation_precision"] is None
