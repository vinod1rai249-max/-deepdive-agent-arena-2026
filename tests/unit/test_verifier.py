"""Phase 3 DoD (BLUEPRINT.md §17): an injected unsupported claim is
rejected with `stage_failed` set, and a mis-cited claim is re-cited
against the sibling passage that actually supports it."""

from __future__ import annotations

import json

import httpx
import respx

from agents.verifier import verify_all
from app.deps import Deps
from app.state import ClaimState, PassageState, SourceState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class _FakeSettings:
    relevance_prefilter_threshold = 0.05


class _FakeRepo:
    def __init__(self) -> None:
        self.verdicts: list = []
        self.claim_updates: list = []

    def create_verdicts(self, verdicts: list) -> None:
        self.verdicts.extend(verdicts)

    def update_claim_statuses(self, claims: list) -> None:
        self.claim_updates.extend(claims)


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
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _source(idx: str, tier: int = 3) -> SourceState:
    return SourceState(
        id=f"source-{idx}", subq_id="subq-1", url=f"https://example.com/{idx}", domain="example.com",
        title=None, tier=tier, content_hash=f"hash-{idx}", text="irrelevant full source text",
        origin="web", published_at=None,
    )


@respx.mock
async def test_unsupported_claim_is_rejected_with_stage_failed(tmp_path):
    passage = PassageState(
        id="passage-1", source_id="source-1", subq_id="subq-1", start=0, end=10,
        text="The company reported revenue growth of 10% in Q1 2025.",
    )
    claim = ClaimState(
        id="claim-1", subq_id="subq-1", text="The company announced a merger with a competitor.",
        passage_id="passage-1", status="pending",
    )

    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(
            json.dumps(
                {
                    "evidence_span": "The company reported revenue growth of 10% in Q1 2025.",
                    "entailment": "unsupported",
                    "reason": "The passage discusses revenue, not a merger.",
                }
            )
        )
    )

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=_FakeSettings())

    updated_claims, verdicts = await verify_all(
        deps, "run-1", [claim], [passage], [_source("1")], already_verified_ids=set()
    )

    assert updated_claims[0]["status"] == "rejected"
    assert verdicts[0]["outcome"] == "rejected"
    assert verdicts[0]["stage_failed"] == "entailment"
    assert repo.verdicts and repo.claim_updates


@respx.mock
async def test_mis_cited_claim_is_re_cited_to_the_sibling_that_supports_it(tmp_path):
    wrong_passage = PassageState(
        id="passage-wrong", source_id="source-1", subq_id="subq-1", start=0, end=10,
        text="The company's headquarters relocated to a new city in 2024.",
    )
    right_passage = PassageState(
        id="passage-right", source_id="source-2", subq_id="subq-1", start=0, end=10,
        text="The company reported revenue growth of 10 percent in Q1 2025.",
    )
    claim = ClaimState(
        id="claim-1", subq_id="subq-1", text="The company's revenue grew 10 percent in Q1 2025.",
        passage_id="passage-wrong", status="pending",
    )

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_content = body["messages"][1]["content"]
        if "relocated" in user_content:
            content = json.dumps(
                {
                    "evidence_span": "The company's headquarters relocated to a new city in 2024.",
                    "entailment": "unsupported",
                    "reason": "This passage is about headquarters relocation, not revenue.",
                }
            )
        else:
            content = json.dumps(
                {
                    "evidence_span": "The company reported revenue growth of 10 percent in Q1 2025.",
                    "entailment": "supported",
                    "reason": "This passage directly states the revenue growth figure.",
                }
            )
        return _mock_response(content)

    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=side_effect)

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=_FakeSettings())

    updated_claims, verdicts = await verify_all(
        deps, "run-1", [claim], [wrong_passage, right_passage], [_source("1"), _source("2")], already_verified_ids=set()
    )

    assert updated_claims[0]["status"] == "re_cited"
    assert updated_claims[0]["passage_id"] == "passage-right"
    assert verdicts[0]["outcome"] == "re_cited"
    assert verdicts[0]["passage_id"] == "passage-right"


@respx.mock
async def test_already_verified_claims_are_skipped(tmp_path):
    passage = PassageState(id="passage-1", source_id="source-1", subq_id="subq-1", start=0, end=10, text="text")
    claim = ClaimState(id="claim-1", subq_id="subq-1", text="a claim", passage_id="passage-1", status="verified")

    route = respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"evidence_span": "text", "entailment": "supported", "reason": "r"}))
    )

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=_FakeSettings())

    updated_claims, verdicts = await verify_all(
        deps, "run-1", [claim], [passage], [_source("1")], already_verified_ids={"claim-1"}
    )

    assert verdicts == []
    assert route.call_count == 0  # no Mesh call spent re-verifying an already-verified claim
    assert updated_claims == [claim]  # unchanged, passed through
