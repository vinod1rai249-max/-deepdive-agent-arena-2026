"""Found during the first live run (DESIGN.md §16): the loop computed
and used real coverage scores internally, but never persisted them to
the SubQuestion.coverage DB column — every sub-question showed 0.00 in
the trail regardless of what the loop actually saw."""

from __future__ import annotations

import pytest

from store.models import Run, SubQuestion
from store.repo import Repo


@pytest.fixture
def repo(tmp_path) -> Repo:
    r = Repo(f"sqlite:///{tmp_path / 'test.db'}")
    r.create_all()
    return r


def test_update_subquestion_coverage_persists_real_scores(repo):
    with repo.session() as session:
        session.add(Run(id="run-1", question="q"))
        session.add(SubQuestion(id="subq-1", run_id="run-1", text="a", acceptance_criteria=["x"], coverage=0.0))
        session.add(SubQuestion(id="subq-2", run_id="run-1", text="b", acceptance_criteria=["y"], coverage=0.0))

    repo.update_subquestion_coverage({"subq-1": 0.83, "subq-2": 0.41})

    with repo.session() as session:
        assert session.get(SubQuestion, "subq-1").coverage == 0.83
        assert session.get(SubQuestion, "subq-2").coverage == 0.41


def test_update_subquestion_coverage_ignores_unknown_ids_without_raising(repo):
    with repo.session() as session:
        session.add(Run(id="run-1", question="q"))
        session.add(SubQuestion(id="subq-1", run_id="run-1", text="a", acceptance_criteria=["x"]))

    repo.update_subquestion_coverage({"subq-1": 0.5, "no-such-subq": 0.9})

    with repo.session() as session:
        assert session.get(SubQuestion, "subq-1").coverage == 0.5
