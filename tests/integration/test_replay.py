"""Phase 5 "Replay mode" (BLUEPRINT.md §13): a completed run's full state
exports to portable JSON and re-imports into a fresh DB with the same
report and trail — no Mesh key or credits needed to inspect it after
the fact. Exercises the real pipeline (mocked Mesh) to produce a
genuine run, then proves export/import round-trips it faithfully."""

from __future__ import annotations

import json

import httpx
import respx

from app.deps import Deps
from app.graph import build_graph, initial_state
from core.config import Settings
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient
from retrieval.base import ScrapedPage, SearchProvider, SearchResult
from store.repo import Repo


class _FakeSearchProvider(SearchProvider):
    name = "fake"

    async def search(self, query: str, limit: int = 5):
        return [SearchResult(url="https://example.com/battery-report", title="Battery Report")]

    async def scrape(self, url: str):
        text = "The global EV battery market reached $50 billion in 2025. " * 10
        return ScrapedPage(url=url, title="Battery Report", text=text, published_at=None)


def _mesh_side_effect(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    system_content = body["messages"][0]["content"]

    if "PlannerOutput" in system_content:
        content = json.dumps(
            {
                "sub_questions": [
                    {
                        "text": "What is the size of the EV battery market?",
                        "acceptance_criteria": ["gives a market size figure"],
                        "time_scope": None,
                        "premise_flag": False,
                    }
                ]
            }
        )
    elif "QueryPlan" in system_content:
        content = json.dumps({"queries": ["EV battery market size"]})
    elif "DrafterOutput" in system_content:
        content = json.dumps({"claims": [{"text": "The EV battery market reached $50 billion in 2025.", "passage_index": 0}]})
    elif "SpanJudgment" in system_content:
        content = json.dumps(
            {
                "evidence_span": "The global EV battery market reached $50 billion in 2025.",
                "entailment": "supported",
                "reason": "Directly stated.",
            }
        )
    elif "CoverageJudgment" in system_content:
        content = json.dumps({"criteria_satisfied": [True], "missing_aspect": None})
    elif "SynthesizerOutput" in system_content:
        content = json.dumps(
            {
                "sections": [
                    {
                        "section": "Market size",
                        "sentences": [{"text": "The EV battery market reached $50 billion in 2025.", "claim_indices": [0]}],
                    }
                ]
            }
        )
    else:
        content = "{}"

    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


@respx.mock
async def test_export_then_import_reproduces_the_same_report_and_trail(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_mesh_side_effect)

    source_db = tmp_path / "source.db"
    repo = Repo(f"sqlite:///{source_db}")
    repo.create_all()

    mesh = MeshClient(
        api_key="rsk_test", cache=DiskCache(str(tmp_path / "cache"), enabled=False),
        breaker=CircuitBreaker(), max_concurrency=2,
    )
    settings = Settings(mesh_api_key="rsk_test", database_url=f"sqlite:///{source_db}")
    deps = Deps(mesh=mesh, search=_FakeSearchProvider(), repo=repo, settings=settings)

    run = repo.create_run(question="What is the size of the EV battery market?")
    graph = build_graph(deps)
    await graph.ainvoke(initial_state(run.id, run.question))

    original_report = repo.get_report(run.id)
    original_trail = repo.get_trail(run.id)
    assert original_report  # sanity: the real pipeline actually produced something

    exported = repo.export_run(run.id)
    export_path = tmp_path / "sample_run.json"
    export_path.write_text(json.dumps(exported), encoding="utf-8")

    # Import into a completely separate, fresh DB — simulating a judge
    # opening a committed sample with no Mesh key at all.
    replay_db = tmp_path / "replay.db"
    replay_repo = Repo(f"sqlite:///{replay_db}")
    replay_repo.create_all()
    reloaded = json.loads(export_path.read_text(encoding="utf-8"))
    replayed_run_id = replay_repo.import_run(reloaded)

    assert replayed_run_id == run.id
    assert replay_repo.get_report(replayed_run_id) == original_report
    assert replay_repo.get_trail(replayed_run_id)["sub_questions"] == original_trail["sub_questions"]
    assert replay_repo.get_trail(replayed_run_id)["stop_reason"] == original_trail["stop_reason"]
