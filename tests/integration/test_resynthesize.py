"""Proves the human-override feature end to end (BLUEPRINT.md §4.1a): a
claim the AI rejected gets excluded from the first report; a human
promotes it with a reason; `resynthesize_run` re-runs Contradiction
Checker -> Synthesizer -> Report Auditor (not the whole pipeline) and
the promoted claim's content reaches the final report."""

from __future__ import annotations

import json

import httpx
import respx

from app.runner import resynthesize_run
from store.models import Claim, Passage, ReportSentence, Run, Source, SubQuestion
from store.repo import Repo


def _seed_run(repo: Repo, run_id: str) -> None:
    with repo.session() as session:
        session.add(Run(id=run_id, question="What happened with the merger?"))
        session.add(
            SubQuestion(
                id="subq-1", run_id=run_id, text="What happened with the merger?",
                acceptance_criteria=["states the outcome"], time_scope=None, premise_flag=False,
            )
        )
        session.add(
            Source(
                id="source-1", run_id=run_id, subq_id="subq-1", url="https://example.com/merger",
                domain="example.com", title="Merger News", tier=3, content_hash="h1",
                text="The merger between the two firms was finalized last month after regulatory approval.",
                origin="web",
            )
        )
        session.add(
            Passage(
                id="passage-1", source_id="source-1", start=0, end=10,
                text="The merger between the two firms was finalized last month after regulatory approval.",
            )
        )
        # This claim was rejected by the AI on the original run (e.g. the
        # Verifier's span-match missed a paraphrase it shouldn't have).
        session.add(
            Claim(
                id="claim-rejected", run_id=run_id, subq_id="subq-1",
                text="The merger was finalized after regulatory approval.",
                passage_id="passage-1", status="rejected",
            )
        )


def _mesh_side_effect(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    system_content = body["messages"][0]["content"]

    if "ContradictionCheckOutput" in system_content:
        content = "{}"
    elif "SynthesizerOutput" in system_content:
        content = json.dumps(
            {
                "sections": [
                    {
                        "section": "Merger outcome",
                        "sentences": [
                            {"text": "The merger was finalized after regulatory approval.", "claim_indices": [0]}
                        ],
                    }
                ]
            }
        )
    elif "SpanJudgment" in system_content:
        content = json.dumps(
            {
                "evidence_span": "The merger between the two firms was finalized last month after regulatory approval.",
                "entailment": "supported",
                "reason": "Directly stated in the passage.",
            }
        )
    else:
        content = "{}"

    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@respx.mock
async def test_promoted_claim_reaches_the_report_after_resynthesis(tmp_path):
    respx.post("https://api.meshapi.ai/v1/chat/completions").mock(side_effect=_mesh_side_effect)

    db_path = tmp_path / "test.db"
    repo = Repo(f"sqlite:///{db_path}")
    repo.create_all()
    _seed_run(repo, "run-1")

    # Before: the rejected claim does not appear in the report at all.
    assert repo.get_report("run-1") == []

    from core.config import Settings

    settings = Settings(
        mesh_api_key="rsk_test", database_url=f"sqlite:///{db_path}",
        llm_cache_dir=str(tmp_path / "llm_cache"), firecrawl_api_key=None, search_provider="null",
    )

    # A human reviews it and disagrees with the AI's rejection.
    repo.override_claim_status("run-1", "claim-rejected", "verified", "Confirmed accurate against the source directly.")

    result = await resynthesize_run(settings, repo, "run-1")

    assert len([s for s in result["report_sentences"] if s["claim_ids"]]) == 1
    assert result["report_sentences"][0]["audit_passed"] is True
    assert "claim-rejected" in result["report_sentences"][0]["claim_ids"]

    # And it's actually persisted — a fresh read reflects the same thing.
    final_report = repo.get_report("run-1")
    cited = [s for s in final_report if s["citations"]]
    assert len(cited) == 1
    assert "finalized" in cited[0]["text"]

    # The override itself is on the record, separate from the AI's verdict.
    reviews = repo.get_human_reviews("run-1")
    assert len(reviews) == 1
    assert reviews[0]["new_status"] == "verified"

    with repo.session() as session:
        claim = session.get(Claim, "claim-rejected")
        assert claim.status == "verified"
        # No duplicate rows from the clear-then-recreate in resynthesize_run.
        # Exactly one row per final sentence (1 cited + 1 gap note): drafts
        # are no longer persisted alongside their audited versions.
        assert session.query(ReportSentence).filter_by(run_id="run-1").count() == len(result["report_sentences"]) == 2
