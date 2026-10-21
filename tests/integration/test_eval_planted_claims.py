"""Proves eval/run_eval.py's planted-claim orchestration actually loads
all 60 authored fixtures from eval/planted_claims.yaml and wires them
through the real agents.verifier.verify_all correctly — using a mocked
Mesh that always judges "unsupported" (a stand-in for a hypothetically
perfect verifier), so this tests the counting/wiring, not what a real
model would actually catch. The real catch rate remains genuinely
pending a live MESH_API_KEY run (BLUEPRINT.md §15) — not claimed here."""

from __future__ import annotations

import json

import httpx
import respx

from app.deps import Deps
from eval.run_eval import compute_planted_claim_recall, evaluate_planted_claims
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


@respx.mock
async def test_planted_claims_orchestration_loads_all_60_fixtures(tmp_path):
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
                                {"evidence_span": "n/a", "entailment": "unsupported", "reason": "simulated perfect verifier"}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )

    mesh = MeshClient(
        api_key="rsk_test", cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(), max_concurrency=4,
    )

    class _Settings:
        relevance_prefilter_threshold = 0.0  # don't let the cheap pre-filter skip any fixture

    class _FakeRepo:
        def create_verdicts(self, verdicts):
            pass

        def update_claim_statuses(self, claims):
            pass

    deps = Deps(mesh=mesh, search=None, repo=_FakeRepo(), settings=_Settings())

    results = await evaluate_planted_claims(deps)

    assert len(results) == 60
    assert {r["type"] for r in results} == {"wrong_number", "wrong_entity", "unsupported_extrapolation", "contradiction"}

    metrics = compute_planted_claim_recall(results)
    assert metrics["overall"] == 1.0  # every claim rejected, since the mock always says "unsupported"
    assert all(v == 1.0 for v in metrics["by_type"].values())
