import json

import httpx
import respx

from agents.coverage import compute_coverage
from app.deps import Deps
from app.state import ClaimState, SubQuestionState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


def _client(tmp_path):
    return MeshClient(
        api_key="rsk_test",
        cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(),
        max_concurrency=2,
    )


def _mock_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


async def test_no_verified_claims_is_zero_coverage_without_calling_mesh():
    subq = SubQuestionState(id="s1", text="q", acceptance_criteria=["a"], time_scope=None, premise_flag=False)
    deps = Deps(mesh=None, search=None, repo=None, settings=None)

    cov, reason = await compute_coverage(deps, "run-1", subq, [], [])
    assert cov == 0.0
    assert reason == "no verified evidence yet"


@respx.mock
async def test_full_coverage_weighted_down_by_low_confidence(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [True], "missing_aspect": None}))
    )

    subq = SubQuestionState(id="s1", text="q", acceptance_criteria=["a"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="s1", text="claim", passage_id="p1", status="verified")
    low_confidence_verdict = {"claim_id": "c1", "confidence_score": 0.2, "confidence_level": "low"}

    deps = Deps(mesh=_client(tmp_path), search=None, repo=None, settings=None)
    cov, _ = await compute_coverage(deps, "run-1", subq, [claim], [low_confidence_verdict])

    assert 0.0 < cov < 1.0  # raw coverage is 1.0 but weighted down by low confidence
