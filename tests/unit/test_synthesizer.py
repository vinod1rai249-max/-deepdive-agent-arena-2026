import httpx
import respx

from agents.synthesizer import synthesize
from app.deps import Deps
from app.state import ClaimState, SubQuestionState
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker
from llm.client import MeshClient


class _FakeRepo:
    def __init__(self) -> None:
        self.sentences: list = []

    def create_report_sentences(self, run_id: str, sentences: list) -> None:
        self.sentences.extend(sentences)


def _client(tmp_path):
    return MeshClient(
        api_key="rsk_test",
        cache=DiskCache(str(tmp_path / "c"), enabled=False),
        breaker=CircuitBreaker(),
        max_concurrency=2,
    )


@respx.mock
async def test_synthesize_resolves_claim_indices_to_real_claim_ids(tmp_path):
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
                            "content": (
                                '{"sections": [{"section": "Demand", "sentences": '
                                '[{"text": "Demand is rising.", "claim_indices": [0]}]}]}'
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )

    subq = SubQuestionState(id="subq-1", text="battery demand trend", acceptance_criteria=["x"], time_scope=None, premise_flag=False)
    claim = ClaimState(id="claim-1", subq_id="subq-1", text="battery demand is rising", passage_id="passage-1")

    repo = _FakeRepo()
    deps = Deps(mesh=_client(tmp_path), search=None, repo=repo, settings=None)

    sentences = await synthesize(deps, "run-1", [subq], [claim])

    assert len(sentences) == 1
    assert sentences[0]["claim_ids"] == ["claim-1"]  # resolved from index in code, never typed by the model


async def test_synthesize_marks_insufficient_evidence_when_no_claims():
    subq = SubQuestionState(id="subq-1", text="no evidence here", acceptance_criteria=["x"], time_scope=None, premise_flag=False)
    repo = _FakeRepo()
    deps = Deps(mesh=None, search=None, repo=repo, settings=None)

    sentences = await synthesize(deps, "run-1", [subq], [])

    assert len(sentences) == 1
    assert sentences[0]["claim_ids"] == []
    assert "Insufficient evidence" in sentences[0]["text"]
