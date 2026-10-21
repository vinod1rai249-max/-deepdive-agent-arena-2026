"""Per-model circuit breaker (BLUEPRINT.md §8/§10). After `failure_threshold`
consecutive failures, a key is "open" for `cooldown_seconds`, during which
MeshClient prefers a role's fallback model instead of hammering the one
that's failing. After cooldown, one trial call is allowed through
(half-open) before the breaker is considered fully reset."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 4, cooldown_seconds: float = 60.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, _State] = {}

    def _state(self, key: str) -> _State:
        return self._states.setdefault(key, _State())

    def is_open(self, key: str) -> bool:
        state = self._state(key)
        if state.opened_at is None:
            return False
        if time.monotonic() - state.opened_at >= self.cooldown_seconds:
            state.opened_at = None
            state.consecutive_failures = 0
            return False
        return True

    def record_success(self, key: str) -> None:
        state = self._state(key)
        state.consecutive_failures = 0
        state.opened_at = None

    def record_failure(self, key: str) -> None:
        state = self._state(key)
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold and state.opened_at is None:
            state.opened_at = time.monotonic()
