"""Per-run budget enforcement (BLUEPRINT.md §6/§10).

Checked at the loop-decision boundary (once per iteration, before
deciding whether to loop again), not before every individual Mesh call
— per-call enforcement is still an explicit Hardening-bucket item
(BLUEPRINT.md §17, item 8). Spend is recomputed from the DB each time
(`Repo.get_run_cost_and_tokens`), not accumulated in memory, so this is
a pure function over current totals rather than a stateful tracker —
simpler to test and matches how it's actually called from
`agents/loop_control.py`.

(An earlier `RunBudget` dataclass existed here, unused anywhere in the
codebase — found during a self-review, DESIGN.md §16. Replaced with
this, which is now actually wired into the loop and has a real
`stop_reason="budget"` test covering it.)
"""

from __future__ import annotations


def budget_exceeded(spent_usd: float, spent_tokens: int, usd_limit: float, token_limit: int) -> bool:
    return spent_usd >= usd_limit or spent_tokens >= token_limit
