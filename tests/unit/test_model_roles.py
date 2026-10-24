"""DESIGN.md §4: the Verifier, Report Auditor and eval Grader must never
share a model — primary OR fallback. A shared fallback silently turns
"independent second opinion" into "same model grading itself" whenever a
circuit breaker flips a role to its fallback (found on the first live
run: 269/297 verifier calls ran on the auditor's model)."""

from __future__ import annotations

from itertools import combinations

import yaml

from llm.client import MODELS_PATH, PRICING_PATH

INDEPENDENT_ROLES = ("verifier", "auditor", "grader")


def _models_for(role_cfg: dict) -> set[str]:
    return {m for m in (role_cfg["model"], role_cfg.get("fallback")) if m}


def _roles() -> dict:
    return yaml.safe_load(open(MODELS_PATH, encoding="utf-8"))["roles"]


def test_verifier_auditor_grader_share_no_model() -> None:
    roles = _roles()
    for a, b in combinations(INDEPENDENT_ROLES, 2):
        shared = _models_for(roles[a]) & _models_for(roles[b])
        assert not shared, f"roles '{a}' and '{b}' share model(s) {shared} (primary or fallback)"


def test_every_configured_model_has_pricing() -> None:
    pricing = yaml.safe_load(open(PRICING_PATH, encoding="utf-8"))
    missing = {m for cfg in _roles().values() for m in _models_for(cfg)} - set(pricing)
    assert not missing, f"models missing from llm/pricing.yaml (cost would use 'default'): {missing}"
