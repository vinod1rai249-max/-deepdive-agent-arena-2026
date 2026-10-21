"""Per-run budget enforcement (BLUEPRINT.md §6/§10).

Scoping note: Phase 3 checks the budget at the loop-decision granularity
(once per iteration, before deciding whether to loop again) rather than
before every individual Mesh call. Per-call enforcement, SSRF test
fixtures, and dedicated budget-cap tests are explicitly Hardening-bucket
items (BLUEPRINT.md §17, item 8) — this gives the loop a real, working
"budget" stop_reason now without the finer-grained plumbing that isn't
on the critical path yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RunBudget:
    usd_limit: float
    token_limit: int
    deadline_s: float
    started_at: float
    spent_usd: float = 0.0
    spent_tokens: int = 0

    @classmethod
    def start(cls, usd_limit: float, token_limit: int, deadline_s: float) -> "RunBudget":
        return cls(usd_limit=usd_limit, token_limit=token_limit, deadline_s=deadline_s, started_at=time.monotonic())

    def record(self, usd: float, tokens: int) -> None:
        self.spent_usd += usd
        self.spent_tokens += tokens

    def exceeded(self) -> bool:
        if self.spent_usd >= self.usd_limit:
            return True
        if self.spent_tokens >= self.token_limit:
            return True
        if time.monotonic() - self.started_at >= self.deadline_s:
            return True
        return False
