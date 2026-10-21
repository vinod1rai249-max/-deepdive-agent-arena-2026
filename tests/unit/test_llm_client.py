"""Proves MeshClient.complete_json works end to end against a mocked Mesh
endpoint: prompt rendering, schema validation, and the disk cache (second
call must not hit the network). No real MESH_API_KEY or credits needed."""

from __future__ import annotations

import httpx
import respx
from pydantic import BaseModel

from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class Ping(BaseModel):
    message: str


def _make_client(tmp_path) -> MeshClient:
    return MeshClient(
        api_key="rsk_test_fake_key",
        base_url="https://api.meshapi.ai/v1",
        cache=DiskCache(directory=str(tmp_path / "cache"), enabled=True),
        breaker=CircuitBreaker(failure_threshold=4, cooldown_seconds=1),
        max_concurrency=2,
    )


@respx.mock
async def test_complete_json_returns_schema_validated_result(tmp_path):
    route = respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"message": "hello world"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
    )

    client = _make_client(tmp_path)
    result = await client.complete_json(
        node="test",
        prompt_name="_example.v1",
        variables={"greeting": "world"},
        schema=Ping,
        model_role="cheap",
    )

    assert isinstance(result, Ping)
    assert result.message == "hello world"
    assert route.call_count == 1


@respx.mock
async def test_complete_json_cache_hit_skips_network(tmp_path):
    route = respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"message": "hello world"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
    )

    client = _make_client(tmp_path)
    kwargs = dict(
        node="test",
        prompt_name="_example.v1",
        variables={"greeting": "world"},
        schema=Ping,
        model_role="cheap",
    )

    first = await client.complete_json(**kwargs)
    second = await client.complete_json(**kwargs)

    assert first.message == second.message == "hello world"
    assert route.call_count == 1  # second call was served from the disk cache
