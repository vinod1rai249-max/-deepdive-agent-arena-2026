"""Follow-up answers come only from stored passages and are re-verified."""

from __future__ import annotations

import json

import httpx
import respx

from agents.followup import NO_EVIDENCE_ANSWER, answer_followup
from app.deps import Deps
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient

PASSAGE = "Global solar capacity reached 660 gigawatts in 2024, a 40% increase."


class _Settings:
    followup_top_k = 5


class _Repo:
    def export_run(self, run_id):
        return {
            "sources": [{"id": "s1", "title": "Solar report", "url": "https://example.org/solar", "tier": 2}],
            "passages": [{"id": "p1", "source_id": "s1", "subq_id": "q", "start": 0, "end": len(PASSAGE), "text": PASSAGE}],
        }


def _resp(content: dict) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "t",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def _deps(tmp_path) -> Deps:
    mesh = MeshClient(api_key="rsk_test", cache=DiskCache(str(tmp_path / "c"), enabled=False), breaker=CircuitBreaker(), max_concurrency=2)
    return Deps(mesh=mesh, search=None, repo=_Repo(), settings=_Settings())


def _handler(answer_sentences: list[dict], span: str, entailment: str = "supported"):
    def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        if "FollowupOutput" in system:
            return _resp({"sentences": answer_sentences})
        return _resp({"evidence_span": span, "entailment": entailment, "reason": "r"})
    return handler


@respx.mock
async def test_supported_sentence_is_returned_with_resolved_citation(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        side_effect=_handler([{"text": "Solar capacity reached 660 gigawatts in 2024.", "passage_indices": [0]}], PASSAGE)
    )
    out = await answer_followup(_deps(tmp_path), "run-1", "How much solar capacity was there?")
    assert [a["citation"]["url"] for a in out["answer"]] == ["https://example.org/solar"]
    assert out["dropped"] == 0 and out["note"] is None


@respx.mock
async def test_sentence_with_a_fabricated_number_is_dropped(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        side_effect=_handler([{"text": "Solar capacity reached 900 gigawatts in 2024.", "passage_indices": [0]}], PASSAGE)
    )
    out = await answer_followup(_deps(tmp_path), "run-1", "How much solar capacity was there?")
    assert out["answer"] == [] and out["dropped"] == 1 and out["note"] == NO_EVIDENCE_ANSWER


@respx.mock
async def test_scope_widened_sentence_is_dropped(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        side_effect=_handler([{"text": "Nationwide solar capacity reached 660 gigawatts in 2024.", "passage_indices": [0]}], PASSAGE)
    )
    out = await answer_followup(_deps(tmp_path), "run-1", "solar capacity?")
    assert out["answer"] == [] and out["dropped"] == 1


@respx.mock
async def test_out_of_range_passage_index_is_dropped_not_guessed(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        side_effect=_handler([{"text": "Solar capacity reached 660 gigawatts in 2024.", "passage_indices": [7]}], PASSAGE)
    )
    out = await answer_followup(_deps(tmp_path), "run-1", "solar capacity?")
    assert out["answer"] == [] and out["dropped"] == 1
