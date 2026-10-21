"""Found on the first live run (DESIGN.md §16): Claude (via Mesh) wraps
JSON output in markdown code fences despite being told not to, and the
client's own fallback logic refused to try a backup model when that
happened — silently breaking verification entirely rather than
degrading gracefully. Both are fixed here."""

from __future__ import annotations

import json

import httpx
import respx

from agents.schemas import SpanJudgment
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient, _strip_markdown_fences


def test_strip_fences_removes_json_code_block():
    fenced = '```json\n{"a": 1}\n```'
    assert _strip_markdown_fences(fenced) == '{"a": 1}'


def test_strip_fences_removes_bare_code_block():
    fenced = '```\n{"a": 1}\n```'
    assert _strip_markdown_fences(fenced) == '{"a": 1}'


def test_strip_fences_leaves_unfenced_json_untouched():
    clean = '{"a": 1}'
    assert _strip_markdown_fences(clean) == clean


def _mock_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@respx.mock
async def test_complete_json_parses_fenced_response_without_needing_repair(tmp_path):
    fenced = '```json\n{"evidence_span": "x", "entailment": "supported", "reason": "r"}\n```'
    route = respx.post("https://api.meshapi.ai/v1/chat/completions").mock(return_value=_mock_response(fenced))

    client = MeshClient(
        api_key="rsk_test", cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(), max_concurrency=2,
    )
    result = await client.complete_json(
        node="test", prompt_name="_example.v1", variables={"greeting": "world"},
        schema=SpanJudgment, model_role="cheap",
    )

    assert result.entailment == "supported"
    assert route.call_count == 1  # no repair call needed once fences are stripped


@respx.mock
async def test_falls_back_to_second_model_when_primary_consistently_fails_schema(tmp_path):
    """A model that fails schema validation for real (not a fence issue)
    should still let the role's fallback model take over — refusing to
    do so silently breaks the whole role, as it did on the first live run."""
    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body["model"] == "openai/gpt-4o":  # the "planner" role's primary
            return _mock_response("not json at all, and no amount of repair will fix it")
        return _mock_response(json.dumps({"evidence_span": "x", "entailment": "supported", "reason": "r"}))

    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=side_effect)

    client = MeshClient(
        api_key="rsk_test", cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(), max_concurrency=2,
    )
    result = await client.complete_json(
        node="test", prompt_name="_example.v1", variables={"greeting": "world"},
        schema=SpanJudgment, model_role="planner",
    )

    assert result.entailment == "supported"  # recovered via the fallback model, not a hard failure
