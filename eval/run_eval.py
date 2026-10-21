"""Self-evaluation harness (BLUEPRINT.md §15). Computes the metrics table
for EVAL.md.

Running `main()` for real needs a valid MESH_API_KEY (and ideally
FIRECRAWL_API_KEY — without it, questions run against NullSearchProvider
and every sub-question ends up "insufficient evidence," which is a real
but uninteresting result) and spends real credits: every question in
questions.yaml runs through the full pipeline, and every planted claim
runs through the Verifier. The disk cache (llm/cache.py) means a re-run
after a partial failure doesn't re-spend credits on calls that already
succeeded.

The metric CALCULATIONS themselves (the `compute_*` functions below) are
pure and unit-tested against constructed data — see
tests/unit/test_eval_metrics.py — independent of whether a real pipeline
run has ever been executed. Nothing in this file fabricates a result;
`main()` has not been run against a real Mesh key as of this commit, and
`eval/results/` is empty until it has.
"""

from __future__ import annotations

import asyncio
import csv
import json
import random
import statistics
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agents.schemas import SpanJudgment
from agents.verifier import verify_all
from app.deps import Deps
from app.runner import build_deps, start_run
from app.state import VERIFIED_STATUSES, ClaimState, PassageState, SourceState
from core.config import get_settings
from eval.baseline import answer_with_citations, score_baseline_answer
from store.repo import Repo

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"
PLANTED_CLAIMS_PATH = EVAL_DIR / "planted_claims.yaml"
GOLD_LABELS_PATH = EVAL_DIR / "gold_labels.csv"
RESULTS_DIR = EVAL_DIR / "results"


# ---------------------------------------------------------------------------
# Pure metric calculations — unit-tested independent of live execution.
# ---------------------------------------------------------------------------


def compute_planted_claim_recall(results: list[dict[str, Any]]) -> dict[str, Any]:
    """`results`: one dict per planted claim, each with at least
    {"type": str, "rejected": bool}. Returns overall recall + per-type."""
    if not results:
        return {"overall": None, "by_type": {}}
    overall = sum(1 for r in results if r["rejected"]) / len(results)
    by_type: dict[str, list[bool]] = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r["rejected"])
    return {
        "overall": round(overall, 4),
        "by_type": {t: round(sum(flags) / len(flags), 4) for t, flags in by_type.items()},
    }


def compute_sentence_support_rate(audited_sentences: list[dict[str, Any]]) -> float | None:
    """Fraction of DRAFTED sentences (before stripping) the auditor judged
    supported. `audited_sentences`: dicts with at least {"audit_passed": bool}."""
    if not audited_sentences:
        return None
    return round(sum(1 for s in audited_sentences if s["audit_passed"]) / len(audited_sentences), 4)


def compute_citation_precision(report_sentences: list[dict[str, Any]], claim_status_by_id: dict[str, str]) -> float | None:
    """Fraction of citations in the FINAL (kept) report whose claim has a
    passing verdict. By construction in this pipeline a citation always
    points to a verified/partial/re_cited claim (the Synthesizer only
    ever sees those) — this exists to catch a regression if that
    invariant is ever broken, not because today's value is expected to
    be below 1.0."""
    total_citations = sum(len(s["claim_ids"]) for s in report_sentences)
    if total_citations == 0:
        return None
    valid = sum(
        1 for s in report_sentences for cid in s["claim_ids"] if claim_status_by_id.get(cid) in VERIFIED_STATUSES
    )
    return round(valid / total_citations, 4)


def compute_rubric_coverage(rubric: list[str], satisfied_flags: list[bool]) -> float:
    if not rubric:
        return 1.0
    padded = (satisfied_flags + [False] * len(rubric))[: len(rubric)]
    return round(sum(1 for f in padded if f) / len(rubric), 4)


def bootstrap_ci(values: list[float], n_resamples: int = 2000, confidence: float = 0.95, seed: int = 0) -> tuple[float, float]:
    """95% CI via bootstrap resampling (BLUEPRINT.md §15 "bootstrap 95% CIs")."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = [statistics.mean(values[rng.randrange(n)] for _ in range(n)) for _ in range(n_resamples)]
    means.sort()
    lower_idx = int((1 - confidence) / 2 * n_resamples)
    upper_idx = min(int((1 + confidence) / 2 * n_resamples), n_resamples - 1)
    return (round(means[lower_idx], 4), round(means[upper_idx], 4))


def cohen_kappa(labels_a: list[str], labels_b: list[str]) -> float:
    """Judge-vs-human agreement (BLUEPRINT.md §15)."""
    if not labels_a or len(labels_a) != len(labels_b):
        return 0.0
    n = len(labels_a)
    categories = sorted(set(labels_a) | set(labels_b))
    observed = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
    expected = sum((labels_a.count(c) / n) * (labels_b.count(c) / n) for c in categories)
    if expected >= 1.0:
        return 1.0
    return round((observed - expected) / (1 - expected), 4)


# ---------------------------------------------------------------------------
# Orchestration — needs a real MESH_API_KEY to execute; spends credits.
# ---------------------------------------------------------------------------


async def evaluate_planted_claims(deps: Deps) -> list[dict[str, Any]]:
    with open(PLANTED_CLAIMS_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    results: list[dict[str, Any]] = []
    for claim_type, entries in data.items():
        for entry in entries:
            source_id = str(uuid.uuid4())
            passage = PassageState(
                id=str(uuid.uuid4()), source_id=source_id, subq_id="eval-subq",
                start=0, end=len(entry["passage"]), text=entry["passage"],
            )
            claim = ClaimState(
                id=str(uuid.uuid4()), subq_id="eval-subq", text=entry["planted_claim"],
                passage_id=passage["id"], status="pending",
            )
            source = SourceState(
                id=source_id, subq_id="eval-subq", url="https://eval.local/fixture", domain="eval.local",
                title=None, tier=3, content_hash="n/a", text=entry["passage"], origin="web", published_at=None,
            )
            updated_claims, _verdicts = await verify_all(deps, "eval-planted", [claim], [passage], [source], set())
            rejected = updated_claims[0]["status"] not in VERIFIED_STATUSES
            results.append(
                {"id": entry["id"], "type": claim_type, "rejected": rejected, "outcome": updated_claims[0]["status"]}
            )
    return results


async def evaluate_gold_labels(
    deps: Deps, repo: Repo | None = None, gold_labels_path: Path | None = None
) -> dict[str, Any]:
    """Judge–human agreement (BLUEPRINT.md §15): runs the independent
    `grader` role (a different model than `verifier`/`auditor` — see
    llm/models.yaml) against every row in gold_labels.csv and compares
    its entailment judgment to the human label. Also computes false-
    rejection rate: of the gold-labeled claims humans marked "supported,"
    what fraction did the production Verifier actually reject (requires
    `repo` so the claim_ids in gold_labels.csv can be looked up against
    real Claim rows from the run they came from).

    Returns `n: 0` and `None` metrics on an empty/header-only CSV —
    never fabricates a number from no data.
    """
    path = gold_labels_path or GOLD_LABELS_PATH
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        return {"n": 0, "accuracy": None, "kappa": None, "false_rejection_rate": None, "details": []}

    grader_labels: list[str] = []
    human_labels: list[str] = []
    details: list[dict[str, Any]] = []
    gold_supported_claim_ids: list[str] = []

    for row in rows:
        human_label = row["human_label"].strip()
        try:
            judgment: SpanJudgment = await deps.mesh.complete_json(
                node="grader",
                prompt_name="grader.v1",
                variables={"claim": row["claim_text"], "passage": row["passage_text"]},
                schema=SpanJudgment,
                model_role="grader",
                run_id="eval-grader",
            )
            grader_label = judgment.entailment
        except Exception:
            grader_label = "unsupported"  # conservative fallback on a call failure, not a silent skip

        grader_labels.append(grader_label)
        human_labels.append(human_label)
        details.append(
            {
                "claim_id": row["claim_id"], "grader_label": grader_label, "human_label": human_label,
                "agree": grader_label == human_label,
            }
        )
        if human_label == "supported":
            gold_supported_claim_ids.append(row["claim_id"])

    accuracy = round(sum(1 for d in details if d["agree"]) / len(details), 4)
    kappa = cohen_kappa(grader_labels, human_labels)

    false_rejection_rate = None
    if repo is not None and gold_supported_claim_ids:
        from store.models import Claim

        with repo.session() as session:
            claims = session.query(Claim).filter(Claim.id.in_(gold_supported_claim_ids)).all()
        if claims:
            # "Rejected" here means "did not end up usable in the report"
            # (BLUEPRINT.md §4; VERIFIED_STATUSES), not only the literal
            # outcome="rejected" — "unverified" and "pending" are the same
            # failure from the gold set's point of view: a claim humans
            # confirmed was supported, that the pipeline never surfaced.
            not_accepted = sum(1 for c in claims if c.status not in VERIFIED_STATUSES)
            false_rejection_rate = round(not_accepted / len(claims), 4)

    return {"n": len(rows), "accuracy": accuracy, "kappa": kappa, "false_rejection_rate": false_rejection_rate, "details": details}


async def evaluate_question(settings: Any, repo: Repo, entry: dict[str, Any]) -> dict[str, Any]:
    run_id = await start_run(settings, repo, entry["question"])
    trail = repo.get_trail(run_id)
    report = repo.get_report(run_id)
    claim_status_by_id = {c["id"]: c["status"] for c in trail["claims"]}
    return {
        "id": entry["id"],
        "run_id": run_id,
        "stop_reason": trail["stop_reason"],
        "iterations": trail["iterations"],
        "cost_usd": trail["llm_calls"]["total_cost_usd"],
        "n_report_sentences": len(report),
        "n_claims": len(trail["claims"]),
        "n_verdicts": len(trail["verdicts"]),
        "citation_precision": compute_citation_precision(report, claim_status_by_id),
    }


async def evaluate_baseline_question(deps: Deps, entry: dict[str, Any]) -> dict[str, Any]:
    """Same question, the baseline (no retrieval, no verification,
    LLM-authored citations) scored by the same standard the Auditor
    holds DeepDive's own sentences to — see eval/baseline.py."""
    answer = await answer_with_citations(deps, "eval-baseline", entry["question"])
    scored = await score_baseline_answer(deps, answer)
    return {"id": entry["id"], **scored}


async def main(limit: int | None = None, with_baseline: bool = True) -> None:
    settings = get_settings()
    repo = Repo(settings.database_url)
    repo.create_all()
    deps = build_deps(settings, repo)

    print("Evaluating planted claims (Verifier only, no search provider needed)...")
    planted_results = await evaluate_planted_claims(deps)
    planted_metrics = compute_planted_claim_recall(planted_results)
    print(f"Planted-claim recall: {planted_metrics}")

    print("Evaluating gold labels (independent grader vs. human — needs gold_labels.csv filled in)...")
    gold_metrics = await evaluate_gold_labels(deps, repo)
    if gold_metrics["n"] == 0:
        print("  gold_labels.csv is empty — run eval/label.py against a real run first. Skipping.")
    else:
        print(f"  n={gold_metrics['n']} accuracy={gold_metrics['accuracy']} kappa={gold_metrics['kappa']} "
              f"false_rejection_rate={gold_metrics['false_rejection_rate']}")

    with open(QUESTIONS_PATH, "r", encoding="utf-8") as fh:
        questions = yaml.safe_load(fh)
    all_entries = [q for domain in questions.get("domains", {}).values() for q in domain]
    if limit:
        all_entries = all_entries[:limit]

    print(f"Running {len(all_entries)} questions through the full pipeline (spends real credits)...")
    question_results = []
    baseline_results = []
    for entry in all_entries:
        result = await evaluate_question(settings, repo, entry)
        question_results.append(result)
        print(f"  {entry['id']}: stop_reason={result['stop_reason']} cost=${result['cost_usd']}")

        if with_baseline:
            baseline_result = await evaluate_baseline_question(deps, entry)
            baseline_results.append(baseline_result)
            print(f"    baseline citation_precision={baseline_result['citation_precision']}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"run_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "timestamp": timestamp,
                "planted_claim_recall": planted_metrics,
                "planted_claim_details": planted_results,
                "gold_label_agreement": gold_metrics,
                "questions": question_results,
                "baseline": baseline_results,
            },
            fh,
            indent=2,
        )
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] != "--no-baseline" else None
    asyncio.run(main(limit=limit_arg, with_baseline="--no-baseline" not in sys.argv))
