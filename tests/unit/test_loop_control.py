"""Phase 3 DoD (BLUEPRINT.md §17): loop fires on a thin sub-question and
records stop_reason — covering each stop_reason path in isolation."""

from __future__ import annotations

import json

import httpx
import respx

from agents.loop_control import decide
from app.deps import Deps
from app.graph import initial_state
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


class _FakeSettings:
    deadline_seconds_per_run = 600
    budget_usd_per_run = 1000.0
    budget_tokens_per_run = 100_000_000


class _FakeRepo:
    def __init__(self, spent_usd: float = 0.0, spent_tokens: int = 0) -> None:
        self.spent_usd = spent_usd
        self.spent_tokens = spent_tokens

    def update_subquestion_coverage(self, coverage: dict) -> None:
        pass

    def get_run_cost_and_tokens(self, run_id: str) -> tuple[float, int]:
        return self.spent_usd, self.spent_tokens


def _state_with(subqs, claims, verdicts, iteration=0, max_iterations=3, no_new_evidence=False):
    state = initial_state("run-1", "question")
    state["sub_questions"] = subqs
    state["claims"] = claims
    state["verdicts"] = verdicts
    state["iteration"] = iteration
    state["max_iterations"] = max_iterations
    state["no_new_evidence_triggered"] = no_new_evidence
    return state


@respx.mock
async def test_loop_fires_and_targets_the_thin_subquestion(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [False], "missing_aspect": "no market size figure yet"}))
    )

    thin_subq = SubQuestionState(id="thin", text="q1", acceptance_criteria=["gives a figure"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="thin", text="claim", passage_id="p1", status="rejected")  # nothing verified

    deps = Deps(mesh=_client(tmp_path), search=None, repo=_FakeRepo(), settings=_FakeSettings())
    state = _state_with([thin_subq], [claim], [], iteration=0, max_iterations=3)

    update = await decide(deps, "run-1", state)

    assert update["stop_reason"] is None
    assert update["research_targets"] == ["thin"]
    assert update["failure_reasons"]["thin"] == "all_rejected"  # no verified claims at all
    assert update["iteration"] == 1


@respx.mock
async def test_stops_with_coverage_met_when_nothing_is_thin(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [True], "missing_aspect": None}))
    )

    subq = SubQuestionState(id="s1", text="q1", acceptance_criteria=["gives a figure"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="s1", text="claim", passage_id="p1", status="verified")
    verdict = {"claim_id": "c1", "confidence_score": 0.9, "confidence_level": "high"}

    deps = Deps(mesh=_client(tmp_path), search=None, repo=_FakeRepo(), settings=_FakeSettings())
    state = _state_with([subq], [claim], [verdict], iteration=0, max_iterations=3)

    update = await decide(deps, "run-1", state)

    assert update["stop_reason"] == "coverage_met"
    assert update["research_targets"] == []


@respx.mock
async def test_stops_with_max_iter_when_iteration_budget_exhausted(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [False], "missing_aspect": "still thin"}))
    )

    subq = SubQuestionState(id="s1", text="q1", acceptance_criteria=["gives a figure"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="s1", text="claim", passage_id="p1", status="rejected")

    deps = Deps(mesh=_client(tmp_path), search=None, repo=_FakeRepo(), settings=_FakeSettings())
    state = _state_with([subq], [claim], [], iteration=2, max_iterations=3)  # next_iteration would be 3 == max

    update = await decide(deps, "run-1", state)

    assert update["stop_reason"] == "max_iter"


@respx.mock
async def test_stops_with_no_new_evidence_even_if_still_thin(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [False], "missing_aspect": "still thin"}))
    )

    subq = SubQuestionState(id="s1", text="q1", acceptance_criteria=["gives a figure"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="s1", text="claim", passage_id="p1", status="rejected")

    deps = Deps(mesh=_client(tmp_path), search=None, repo=_FakeRepo(), settings=_FakeSettings())
    state = _state_with([subq], [claim], [], iteration=0, max_iterations=3, no_new_evidence=True)

    update = await decide(deps, "run-1", state)

    assert update["stop_reason"] == "no_new_evidence"


@respx.mock
async def test_stops_with_budget_when_spend_exceeds_the_cap(tmp_path):
    """BLUEPRINT.md §6/§10: the loop checks real spend recomputed from
    the DB, not a bare retry-count — found unwired during a self-review
    (DESIGN.md §16) and fixed here."""
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [False], "missing_aspect": "still thin"}))
    )

    subq = SubQuestionState(id="s1", text="q1", acceptance_criteria=["gives a figure"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="s1", text="claim", passage_id="p1", status="rejected")

    class _OverBudgetSettings(_FakeSettings):
        budget_usd_per_run = 1.0

    deps = Deps(mesh=_client(tmp_path), search=None, repo=_FakeRepo(spent_usd=1.5), settings=_OverBudgetSettings())
    state = _state_with([subq], [claim], [], iteration=0, max_iterations=3)

    update = await decide(deps, "run-1", state)

    assert update["stop_reason"] == "budget"


@respx.mock
async def test_does_not_stop_with_budget_when_under_the_cap(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(
        return_value=_mock_response(json.dumps({"criteria_satisfied": [True], "missing_aspect": None}))
    )

    subq = SubQuestionState(id="s1", text="q1", acceptance_criteria=["gives a figure"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="c1", subq_id="s1", text="claim", passage_id="p1", status="verified")
    verdict = {"claim_id": "c1", "confidence_score": 0.9, "confidence_level": "high"}

    deps = Deps(mesh=_client(tmp_path), search=None, repo=_FakeRepo(spent_usd=0.01), settings=_FakeSettings())
    state = _state_with([subq], [claim], [verdict], iteration=0, max_iterations=3)

    update = await decide(deps, "run-1", state)

    assert update["stop_reason"] == "coverage_met"  # not "budget" — plenty of headroom left
