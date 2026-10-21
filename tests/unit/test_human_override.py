"""Human-in-the-loop override (BLUEPRINT.md §4.1a): a human promotes or
confirms a claim's status without mutating the original AI verdict."""

from __future__ import annotations

import pytest

from store.models import Claim, HumanReview, Run
from store.repo import Repo


@pytest.fixture
def repo(tmp_path) -> Repo:
    r = Repo(f"sqlite:///{tmp_path / 'test.db'}")
    r.create_all()
    return r


def _seed_claim(repo: Repo, run_id: str, status: str = "rejected") -> str:
    with repo.session() as session:
        session.add(Run(id=run_id, question="q"))
        session.add(Claim(id="claim-1", run_id=run_id, subq_id="subq-1", text="a claim", passage_id="p1", status=status))
    return "claim-1"


def test_override_updates_claim_status_and_records_a_human_review(repo):
    claim_id = _seed_claim(repo, "run-1", status="rejected")

    repo.override_claim_status("run-1", claim_id, "verified", "I checked the source directly, this is accurate")

    with repo.session() as session:
        claim = session.get(Claim, claim_id)
        assert claim.status == "verified"

    reviews = repo.get_human_reviews("run-1")
    assert len(reviews) == 1
    assert reviews[0]["previous_status"] == "rejected"
    assert reviews[0]["new_status"] == "verified"
    assert "checked the source" in reviews[0]["reason"]


def test_override_does_not_mutate_the_original_verdict(repo):
    """The AI's Verdict row is a separate table from Claim.status and
    HumanReview — overriding never touches it, so the original AI
    reasoning stays visible next to the human's decision."""
    from store.models import Verdict

    claim_id = _seed_claim(repo, "run-1", status="rejected")
    with repo.session() as session:
        session.add(
            Verdict(
                claim_id=claim_id, stage_reached="reattribution", stage_failed="entailment", outcome="rejected",
                entailment="unsupported", evidence_span="original span", reason="AI's original reasoning",
                model="verifier", prompt_version="verifier.v1",
            )
        )

    repo.override_claim_status("run-1", claim_id, "verified", "human disagrees with the AI")

    with repo.session() as session:
        verdict = session.query(Verdict).filter_by(claim_id=claim_id).one()
        assert verdict.outcome == "rejected"  # untouched
        assert verdict.reason == "AI's original reasoning"  # untouched


def test_override_raises_for_unknown_claim(repo):
    with repo.session() as session:
        session.add(Run(id="run-1", question="q"))

    with pytest.raises(ValueError):
        repo.override_claim_status("run-1", "no-such-claim", "verified", "reason")


def test_override_raises_when_claim_belongs_to_a_different_run(repo):
    claim_id = _seed_claim(repo, "run-1")
    with repo.session() as session:
        session.add(Run(id="run-2", question="other"))

    with pytest.raises(ValueError):
        repo.override_claim_status("run-2", claim_id, "verified", "reason")


def test_clear_report_sentences_and_contradictions(repo):
    from store.models import Contradiction, ReportSentence

    with repo.session() as session:
        session.add(Run(id="run-1", question="q"))
        session.add(ReportSentence(run_id="run-1", section="s", order=0, text="t", claim_ids=["c1"], audit_passed=True))
        session.add(Contradiction(run_id="run-1", subq_id="subq-1", claim_a="c1", claim_b="c2", explanation="e"))

    repo.clear_report_sentences("run-1")
    repo.clear_contradictions("run-1")

    with repo.session() as session:
        assert session.query(ReportSentence).filter_by(run_id="run-1").count() == 0
        assert session.query(Contradiction).filter_by(run_id="run-1").count() == 0
