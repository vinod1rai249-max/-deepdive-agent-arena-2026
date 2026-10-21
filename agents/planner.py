"""Planner agent (BLUEPRINT.md §4.1): breaks the question into sub-
questions, each with acceptance criteria a downstream agent can check
coverage against, and flags a false/loaded premise in the original
question for a direct premise-check sub-question."""

from __future__ import annotations

import uuid

from agents.schemas import PlannerOutput
from app.deps import Deps
from app.state import SubQuestionState


async def plan(deps: Deps, run_id: str, question: str) -> list[SubQuestionState]:
    output: PlannerOutput = await deps.mesh.complete_json(
        node="planner",
        prompt_name="planner.v1",
        variables={"question": question},
        schema=PlannerOutput,
        model_role="planner",
        run_id=run_id,
    )

    sub_questions: list[SubQuestionState] = [
        SubQuestionState(
            id=str(uuid.uuid4()),
            text=sq.text,
            acceptance_criteria=sq.acceptance_criteria,
            time_scope=sq.time_scope,
            premise_flag=sq.premise_flag,
        )
        for sq in output.sub_questions
    ]

    deps.repo.create_subquestions(run_id, sub_questions)
    return sub_questions
