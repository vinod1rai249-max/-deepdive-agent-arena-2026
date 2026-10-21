import httpx
import respx

from agents.drafter import draft_all
from app.deps import Deps
from app.state import PassageState, SubQuestionState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class _FakeSettings:
    max_passages_per_call = 12


class _FakeRepo:
    def __init__(self) -> None:
        self.claims: list = []

    def create_claims(self, run_id: str, claims: list) -> None:
        self.claims.extend(claims)


def _client(tmp_path):
    return MeshClient(
        api_key="rsk_test",
        cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(),
        max_concurrency=2,
    )


@respx.mock
async def test_draft_resolves_passage_index_to_real_passage_id(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"claims": [{"text": "battery demand is rising", "passage_index": 0}]}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )

    subq = SubQuestionState(
        id="subq-1", text="battery demand trend", acceptance_criteria=["identifies a trend"],
        time_scope=None, premise_flag=False,
    )
    passage = PassageState(
        id="passage-1", source_id="source-1", subq_id="subq-1",
        start=0, end=10, text="Battery demand is rising fast this year.",
    )

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=_FakeSettings())

    claims = await draft_all(deps, "run-1", [subq], [passage])

    assert len(claims) == 1
    assert claims[0]["passage_id"] == "passage-1"  # resolved from index in code, never typed by the model
    assert repo.claims == claims


@respx.mock
async def test_draft_drops_out_of_range_passage_index(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"claims": [{"text": "hallucinated claim", "passage_index": 99}]}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )

    subq = SubQuestionState(
        id="subq-1", text="battery demand trend", acceptance_criteria=["identifies a trend"],
        time_scope=None, premise_flag=False,
    )
    passage = PassageState(
        id="passage-1", source_id="source-1", subq_id="subq-1",
        start=0, end=10, text="Battery demand is rising fast this year.",
    )

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=_FakeSettings())

    claims = await draft_all(deps, "run-1", [subq], [passage])

    assert claims == []  # an index we never offered is dropped, not guessed at
