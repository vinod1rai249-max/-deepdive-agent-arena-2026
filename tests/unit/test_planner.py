import httpx
import respx

from agents.planner import plan
from app.deps import Deps
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class _FakeRepo:
    def __init__(self) -> None:
        self.saved: list = []

    def create_subquestions(self, run_id: str, sub_questions: list) -> None:
        self.saved = sub_questions


def _client(tmp_path):
    return MeshClient(
        api_key="rsk_test",
        cache=DiskCache(str(tmp_path / "cache"), enabled=False),
        breaker=CircuitBreaker(),
        max_concurrency=2,
    )


def _mock_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@respx.mock
async def test_plan_produces_subquestions_with_server_assigned_ids(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(
            '{"sub_questions": [{"text": "What is X?", "acceptance_criteria": ["gives a figure"], '
            '"time_scope": null, "premise_flag": false}]}'
        )
    )

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    result = await plan(deps, "run-1", "What is X?")

    assert len(result) == 1
    assert result[0]["text"] == "What is X?"
    assert result[0]["id"]  # a real uuid assigned by our code, never typed by the model
    assert repo.saved == result
