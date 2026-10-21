"""Phase 2 DoD (BLUEPRINT.md §17): a fixture with >30 passages for one
sub-question proves the Curator cap trims and logs the drop rather than
passing everything downstream."""

from __future__ import annotations

import logging

from agents.curator import curate
from app.deps import Deps
from app.state import SourceState, SubQuestionState


class _FakeRepo:
    def __init__(self) -> None:
        self.passages: list = []

    def create_passages(self, passages: list) -> None:
        self.passages.extend(passages)


class _FakeSettings:
    max_passages_per_subquestion = 30


def _make_source(idx: int, tier: int = 3) -> SourceState:
    # Long, mutually-distinct text so chunking yields several passages per
    # source and dedup doesn't collapse them into each other.
    text = " ".join(
        f"Fact number {idx}-{i} about battery supply chains and raw material risk." for i in range(50)
    )
    return SourceState(
        id=f"source-{idx}",
        subq_id="subq-1",
        url=f"https://example.com/{idx}",
        domain="example.com",
        title=None,
        tier=tier,
        content_hash=f"hash-{idx}",
        text=text,
        origin="web",
        published_at=None,
    )


def test_curator_trims_and_logs_when_over_cap(caplog):
    subq = SubQuestionState(
        id="subq-1",
        text="What are battery supply chain risks?",
        acceptance_criteria=["identifies at least one raw material risk"],
        time_scope=None,
        premise_flag=False,
    )
    # 10 distinct, long sources chunk into well over 30 passages combined.
    sources = [_make_source(i) for i in range(10)]

    repo = _FakeRepo()
    deps = Deps(mesh=None, search=None, repo=repo, settings=_FakeSettings())

    caplog.set_level(logging.WARNING)
    passages = curate(deps, "run-1", [subq], sources)

    assert len(passages) <= 30
    assert "trimmed passages over max_passages_per_subquestion" in caplog.text
    assert len(repo.passages) == len(passages)  # persisted exactly what was kept, nothing silently lost from the record


def test_curator_does_not_trim_when_under_cap():
    subq = SubQuestionState(
        id="subq-1",
        text="What are battery supply chain risks?",
        acceptance_criteria=["identifies at least one raw material risk"],
        time_scope=None,
        premise_flag=False,
    )
    sources = [_make_source(0)]  # one source, well under the cap once chunked

    repo = _FakeRepo()
    deps = Deps(mesh=None, search=None, repo=repo, settings=_FakeSettings())

    passages = curate(deps, "run-1", [subq], sources)

    assert 0 < len(passages) <= 30
    assert len(repo.passages) == len(passages)
