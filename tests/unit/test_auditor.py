"""Phase 4 DoD (BLUEPRINT.md §17): every rendered sentence has
audit_passed=True or is stripped. Covers a clean pass, an embellished
sentence that fails then passes after the deterministic rewrite, and a
sentence that still fails after rewrite and gets stripped."""

from __future__ import annotations

import json

import httpx
import respx

from agents.auditor import audit_all
from app.deps import Deps
from app.state import ClaimState, PassageState, ReportSentenceState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class _FakeRepo:
    def __init__(self) -> None:
        self.saved: list = []

    def create_report_sentences(self, run_id: str, sentences: list) -> None:
        self.saved.extend(sentences)


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


_PASSAGE = PassageState(
    id="passage-1", source_id="source-1", subq_id="subq-1", start=0, end=10,
    text="The company's revenue grew by 10 percent in Q1 2025.",
)
_CLAIM = ClaimState(
    id="claim-1", subq_id="subq-1", text="The company's revenue grew by 10 percent in Q1 2025.",
    passage_id="passage-1", status="verified",
)


@respx.mock
async def test_accurate_sentence_passes_cleanly(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(
            json.dumps(
                {
                    "evidence_span": "The company's revenue grew by 10 percent in Q1 2025.",
                    "entailment": "supported",
                    "reason": "Matches the evidence exactly.",
                }
            )
        )
    )

    sentence = ReportSentenceState(
        section="Revenue", order=0, text="The company's revenue grew by 10 percent in Q1 2025.",
        claim_ids=["claim-1"], audit_passed=False, audit_reason=None,
    )
    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    final = await audit_all(deps, "run-1", [sentence], [_CLAIM], [_PASSAGE])

    assert len(final) == 1
    assert final[0]["audit_passed"] is True
    assert repo.saved[0]["audit_passed"] is True


@respx.mock
async def test_embellished_sentence_is_rewritten_and_then_passes(tmp_path):
    call_count = {"n": 0}

    def side_effect(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        body = json.loads(request.content.decode("utf-8"))
        sentence_in_prompt = body["messages"][1]["content"]
        if "massively outperformed every competitor" in sentence_in_prompt:
            # Round 1: the embellished original sentence fails.
            content = json.dumps(
                {
                    "evidence_span": "The company's revenue grew by 10 percent in Q1 2025.",
                    "entailment": "unsupported",
                    "reason": "The evidence doesn't mention competitors at all.",
                }
            )
        else:
            # Round 2: the deterministic rewrite (the claim text verbatim) passes.
            content = json.dumps(
                {
                    "evidence_span": "The company's revenue grew by 10 percent in Q1 2025.",
                    "entailment": "supported",
                    "reason": "Matches the evidence exactly.",
                }
            )
        return _mock_response(content)

    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=side_effect)

    embellished = ReportSentenceState(
        section="Revenue", order=0,
        text="The company massively outperformed every competitor thanks to 10 percent revenue growth in Q1 2025.",
        claim_ids=["claim-1"], audit_passed=False, audit_reason=None,
    )
    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    final = await audit_all(deps, "run-1", [embellished], [_CLAIM], [_PASSAGE])

    assert call_count["n"] == 2  # one failed attempt, one rewrite-and-retry
    assert len(final) == 1
    assert final[0]["audit_passed"] is True
    assert final[0]["text"] == _CLAIM["text"]  # fell back to the verbatim claim text


@respx.mock
async def test_sentence_failing_even_after_rewrite_is_stripped_but_persisted(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(
            json.dumps(
                {
                    "evidence_span": "The company's revenue grew by 10 percent in Q1 2025.",
                    "entailment": "unsupported",
                    "reason": "Never actually supports this sentence, no matter how it's phrased.",
                }
            )
        )
    )

    sentence = ReportSentenceState(
        section="Revenue", order=0, text="Something unrelated and wrong.",
        claim_ids=["claim-1"], audit_passed=False, audit_reason=None,
    )
    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    final = await audit_all(deps, "run-1", [sentence], [_CLAIM], [_PASSAGE])

    assert final == []  # stripped from the rendered report
    assert len(repo.saved) == 1  # but never silently — the outcome is persisted
    assert repo.saved[0]["audit_passed"] is False


@respx.mock
async def test_gap_placeholder_with_no_citations_passes_through_untouched(tmp_path):
    route = respx.post("https://api.meshapi.ai/v1/chat/completions")

    placeholder = ReportSentenceState(
        section="Some sub-question", order=0, text="Insufficient evidence was found for this sub-question.",
        claim_ids=[], audit_passed=False, audit_reason="no verified claims for this sub-question",
    )
    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    final = await audit_all(deps, "run-1", [placeholder], [], [])

    assert len(final) == 1
    assert final[0]["audit_passed"] is True
    assert not route.calls  # never spends a Mesh call auditing a sentence with nothing to cite
