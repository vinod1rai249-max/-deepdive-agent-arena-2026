"""Hardening bonus (BLUEPRINT.md §17 item 7): a contradiction between two
verified claims is flagged with both sources recorded, never silently
resolved."""

from __future__ import annotations

import json

import httpx
import respx

from agents.contradiction import check_all
from app.deps import Deps
from app.state import ClaimState, SubQuestionState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class _FakeRepo:
    def __init__(self) -> None:
        self.saved: list = []

    def create_contradictions(self, run_id: str, contradictions: list) -> None:
        self.saved.extend(contradictions)


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


@respx.mock
async def test_contradicting_claims_are_flagged_with_both_sources(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(
            json.dumps({"contradictions": [{"claim_index_a": 0, "claim_index_b": 1, "explanation": "different figures for the same metric"}]})
        )
    )

    subq = SubQuestionState(id="subq-1", text="market size", acceptance_criteria=["x"], time_scope=None, premise_flag=False)
    claim_a = ClaimState(id="claim-a", subq_id="subq-1", text="The market is worth $50 billion.", passage_id="p1", status="verified")
    claim_b = ClaimState(id="claim-b", subq_id="subq-1", text="The market is worth $80 billion.", passage_id="p2", status="verified")

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    result = await check_all(deps, "run-1", [subq], [claim_a, claim_b])

    assert len(result) == 1
    assert {result[0]["claim_a"], result[0]["claim_b"]} == {"claim-a", "claim-b"}
    assert repo.saved == result


async def test_single_claim_skips_mesh_call_entirely(tmp_path):
    subq = SubQuestionState(id="subq-1", text="market size", acceptance_criteria=["x"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="claim-a", subq_id="subq-1", text="x", passage_id="p1", status="verified")

    repo = _FakeRepo()
    deps = Deps(mesh=None, search=None, repo=repo, settings=None)

    result = await check_all(deps, "run-1", [subq], [claim])

    assert result == []


async def test_only_verified_claims_are_considered():
    subq = SubQuestionState(id="subq-1", text="market size", acceptance_criteria=["x"], time_scope=None, premise_flag=False)
    rejected_a = ClaimState(id="claim-a", subq_id="subq-1", text="x", passage_id="p1", status="rejected")
    rejected_b = ClaimState(id="claim-b", subq_id="subq-1", text="y", passage_id="p2", status="unverified")

    repo = _FakeRepo()
    deps = Deps(mesh=None, search=None, repo=repo, settings=None)

    result = await check_all(deps, "run-1", [subq], [rejected_a, rejected_b])

    assert result == []  # never even reaches the <2-claims-skip, both are filtered out first
