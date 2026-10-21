"""Tests the eval grader orchestration (BLUEPRINT.md §15 — the gap
closed per the user's request): judge-human agreement computed from a
real (temp) gold_labels.csv against a mocked `grader`-role Mesh call,
plus false-rejection-rate computed against seeded Claim rows."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import respx

from app.deps import Deps
from eval.run_eval import evaluate_gold_labels
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient
from store.models import Claim, Run
from store.repo import Repo


def _write_gold_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["claim_id", "claim_text", "passage_id", "passage_text", "human_label", "notes"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _client(tmp_path) -> MeshClient:
    return MeshClient(
        api_key="rsk_test", cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(), max_concurrency=2,
    )


async def test_empty_gold_labels_file_returns_none_without_calling_mesh(tmp_path):
    path = tmp_path / "gold.csv"
    _write_gold_csv(path, [])

    deps = Deps(mesh=None, search=None, repo=None, settings=None)
    result = await evaluate_gold_labels(deps, repo=None, gold_labels_path=path)

    assert result == {"n": 0, "accuracy": None, "kappa": None, "false_rejection_rate": None, "details": []}


@respx.mock
async def test_accuracy_and_kappa_computed_against_human_labels(tmp_path):
    path = tmp_path / "gold.csv"
    _write_gold_csv(
        path,
        [
            {"claim_id": "c1", "claim_text": "Revenue grew 10%.", "passage_id": "p1", "passage_text": "Revenue grew 10% this quarter.", "human_label": "supported", "notes": ""},
            {"claim_id": "c2", "claim_text": "The company merged.", "passage_id": "p2", "passage_text": "Unrelated text about weather.", "human_label": "unsupported", "notes": ""},
        ],
    )

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_content = body["messages"][1]["content"]
        if "Revenue" in user_content:
            content = json.dumps({"evidence_span": "Revenue grew 10% this quarter.", "entailment": "supported", "reason": "matches"})
        else:
            content = json.dumps({"evidence_span": "Unrelated text about weather.", "entailment": "unsupported", "reason": "no mention of a merger"})
        return httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=side_effect)

    deps = Deps(mesh=_client(tmp_path), search=None, repo=None, settings=None)
    result = await evaluate_gold_labels(deps, repo=None, gold_labels_path=path)

    assert result["n"] == 2
    assert result["accuracy"] == 1.0  # grader agreed with the human on both
    assert result["kappa"] == 1.0
    assert result["details"][0]["grader_label"] == "supported"
    assert result["details"][1]["grader_label"] == "unsupported"


@respx.mock
async def test_false_rejection_rate_against_seeded_claims(tmp_path):
    path = tmp_path / "gold.csv"
    _write_gold_csv(
        path,
        [
            {"claim_id": "claim-A", "claim_text": "x", "passage_id": "p1", "passage_text": "y", "human_label": "supported", "notes": ""},
            {"claim_id": "claim-B", "claim_text": "x", "passage_id": "p1", "passage_text": "y", "human_label": "supported", "notes": ""},
        ],
    )
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"evidence_span": "y", "entailment": "supported", "reason": "r"})}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )

    repo = Repo(f"sqlite:///{tmp_path / 'test.db'}")
    repo.create_all()
    with repo.session() as session:
        session.add(Run(id="run-1", question="q"))
        # Humans say both are "supported," but the production pipeline
        # only kept one of them — this is exactly what false-rejection
        # rate is supposed to catch.
        session.add(Claim(id="claim-A", run_id="run-1", subq_id="s1", text="x", passage_id="p1", status="verified"))
        session.add(Claim(id="claim-B", run_id="run-1", subq_id="s1", text="x", passage_id="p1", status="rejected"))

    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)
    result = await evaluate_gold_labels(deps, repo=repo, gold_labels_path=path)

    assert result["false_rejection_rate"] == 0.5
