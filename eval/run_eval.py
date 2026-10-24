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
`eval/results/` holds the JSON from each real execution of `main()`;
EVAL.md quotes those files, never numbers typed by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agents.schemas import CoverageJudgment, SpanJudgment
from agents.verifier import verify_all
from app.deps import Deps
from app.runner import build_deps, start_run
from app.state import VERIFIED_STATUSES, ClaimState, PassageState, SourceState
from core.config import get_settings
from core.guardrails import wrap_untrusted
from eval.baseline import answer_with_citations, score_baseline_answer
from eval.injection import CASES, StaticPageProvider, outputs_follow_injection, verify_false_claim
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
            updated_claims, verdicts = await verify_all(deps, "eval-planted", [claim], [passage], [source], set())
            rejected = updated_claims[0]["status"] not in VERIFIED_STATUSES
            results.append(
                {
                    "id": entry["id"], "type": claim_type, "rejected": rejected, "outcome": updated_claims[0]["status"],
                    "stage_failed": verdicts[0]["stage_failed"] if verdicts else None,
                }
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
        # Rows with a blank/invalid human_label are unlabelled candidates, not data.
        rows = [r for r in csv.DictReader(fh) if r["human_label"].strip() in ("supported", "partial", "unsupported", "contradicted")]

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


async def grade_final_report(deps: Deps, export: dict[str, Any]) -> dict[str, Any]:
    """Independent re-grade of every CITED sentence in the final report by
    the `grader` role — a model that is neither the Verifier nor the
    Auditor (tests/unit/test_model_roles.py), so this number is not the
    pipeline marking its own homework. Evidence = the stored passage
    behind each cited claim; the grader sees nothing of the pipeline's
    verdicts. `strict` counts only "supported"; `lenient` also counts
    "partial"."""
    passage_text = {p["id"]: p["text"] for p in export["passages"]}
    claim_by_id = {c["id"]: c for c in export["claims"]}
    cited = [s for s in export["report_sentences"] if s["claim_ids"] and s["audit_passed"]]

    async def grade(sentence: dict[str, Any]) -> str:
        evidence = "\n\n".join(
            dict.fromkeys(
                passage_text[claim_by_id[cid]["passage_id"]]
                for cid in sentence["claim_ids"]
                if cid in claim_by_id and claim_by_id[cid]["passage_id"] in passage_text
            )
        )
        if not evidence:
            return "unsupported"
        try:
            judgment: SpanJudgment = await deps.mesh.complete_json(
                node="grader", prompt_name="grader.v1",
                variables={"claim": sentence["text"], "passage": wrap_untrusted(evidence)},
                schema=SpanJudgment, model_role="grader", run_id="eval-grader",
            )
            return judgment.entailment
        except Exception:
            return "grader_error"

    labels = await asyncio.gather(*(grade(s) for s in cited))
    n = len(labels)
    return {
        "n_cited_sentences": n,
        "strict_support_rate": round(sum(1 for l in labels if l == "supported") / n, 4) if n else None,
        "lenient_support_rate": round(sum(1 for l in labels if l in ("supported", "partial")) / n, 4) if n else None,
        "grader_errors": sum(1 for l in labels if l == "grader_error"),
        "labels": list(labels),
    }


async def grade_rubric(deps: Deps, entry: dict[str, Any], export: dict[str, Any]) -> float | None:
    """Rubric coverage (BLUEPRINT.md §15): the rubric was written before any
    run (questions.yaml). The independent `grader` role judges which rubric
    aspects the FINAL cited report text actually covers."""
    rubric = entry.get("rubric") or []
    report_text = [s["text"] for s in export["report_sentences"] if s["claim_ids"] and s["audit_passed"]]
    if not rubric:
        return None
    if not report_text:
        return 0.0
    try:
        judgment: CoverageJudgment = await deps.mesh.complete_json(
            node="grader", prompt_name="coverage.v1",
            variables={
                "sub_question": entry["question"],
                "acceptance_criteria": "\n".join(f"{i}. {r}" for i, r in enumerate(rubric)),
                "claims": "\n".join(f"- {t}" for t in report_text),
            },
            schema=CoverageJudgment, model_role="grader", run_id="eval-grader",
        )
    except Exception:
        return None
    return compute_rubric_coverage(rubric, judgment.criteria_satisfied)


async def evaluate_injection(settings: Any, repo: Repo, deps: Deps) -> dict[str, Any]:
    """Injection resistance (BLUEPRINT.md §15; target 0 followed). For each
    fixture page: (a) a real pipeline run where that page is the only
    source, checked for the attack's canary phrases in anything a reader
    sees; (b) a deliberately false claim about the page pushed through the
    real Verifier, which must reject it. See eval/injection.py."""
    details: list[dict[str, Any]] = []
    for name, case in CASES.items():
        provider = StaticPageProvider(name)
        run_id = await start_run(settings, repo, case["question"], search=provider)
        export = repo.export_run(run_id)
        canaries_found = outputs_follow_injection(export, case["canaries"])
        false_claim_accepted = await verify_false_claim(deps, name, case["false_claim"])
        details.append(
            {
                "page": name, "run_id": run_id, "canaries_found": canaries_found,
                "false_claim_accepted": false_claim_accepted,
                "n_claims": len(export["claims"]),
                "n_report_sentences": sum(1 for r in export["report_sentences"] if r["claim_ids"] and r["audit_passed"]),
                "followed": bool(canaries_found) or false_claim_accepted,
            }
        )
        print(f"  injection {name}: followed={details[-1]['followed']} canaries={canaries_found} false_claim_accepted={false_claim_accepted}")
    return {"n": len(details), "followed": sum(1 for d in details if d["followed"]), "details": details}


async def evaluate_question(settings: Any, repo: Repo, entry: dict[str, Any], deps: Deps | None = None) -> dict[str, Any]:
    started = time.monotonic()
    run_id = await start_run(settings, repo, entry["question"])
    latency_s = round(time.monotonic() - started, 1)
    trail = repo.get_trail(run_id)
    export = repo.export_run(run_id)
    report = repo.get_report(run_id)  # resolved view (titles/urls); has no claim_ids
    claim_status_by_id = {c["id"]: c["status"] for c in trail["claims"]}

    statuses = [c["status"] for c in trail["claims"]]
    n_claims = len(statuses)
    n_passed = sum(1 for st in statuses if st in VERIFIED_STATUSES)
    cited_rows = [s for s in export["report_sentences"] if s["claim_ids"]]
    gap_rows = [s for s in export["report_sentences"] if not s["claim_ids"]]

    result: dict[str, Any] = {
        "id": entry["id"],
        "run_id": run_id,
        "stop_reason": trail["stop_reason"],
        "iterations": trail["iterations"],
        "cost_usd": trail["llm_calls"]["total_cost_usd"],
        "latency_s": latency_s,
        "n_report_sentences": len(report),
        "n_claims": n_claims,
        "n_claims_passed": n_passed,
        "claim_pass_rate": round(n_passed / n_claims, 4) if n_claims else None,
        "n_claims_rejected": sum(1 for st in statuses if st == "rejected"),
        "n_verdicts": len(trail["verdicts"]),
        # Fraction of synthesized, cited sentences the Report Auditor kept (before any stripping).
        "sentence_support_rate": compute_sentence_support_rate(cited_rows),
        "n_gap_notes": len(gap_rows),
        "citation_precision": compute_citation_precision(
            [s for s in export["report_sentences"] if s["claim_ids"] and s["audit_passed"]], claim_status_by_id
        ),
        "premise_flagged": any(sq["premise_flag"] for sq in trail["sub_questions"]),
    }
    if deps is not None:
        result["independent_grade"] = await grade_final_report(deps, export)
        result["rubric_coverage"] = await grade_rubric(deps, entry, export)
    return result


async def evaluate_baseline_question(deps: Deps, entry: dict[str, Any]) -> dict[str, Any]:
    """Same question, the baseline (no retrieval, no verification,
    LLM-authored citations) scored by the same standard the Auditor
    holds DeepDive's own sentences to — see eval/baseline.py."""
    answer = await answer_with_citations(deps, "eval-baseline", entry["question"])
    scored = await score_baseline_answer(deps, answer)
    return {"id": entry["id"], **scored}


def _select_entries(questions: dict[str, Any], per_domain: int | None, limit: int | None, include_premise: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for domain in questions.get("domains", {}).values():
        entries.extend(domain[:per_domain] if per_domain else domain)
    if limit:
        entries = entries[:limit]
    if include_premise:
        entries.extend({**q, "is_premise": True} for q in questions.get("false_premise", []))
    return entries


async def main(
    per_domain: int | None = None, limit: int | None = None, with_baseline: bool = True,
    include_premise: bool = True, concurrency: int = 3, resume_path: Path | None = None,
    planted_only: bool = False, injection_only: bool = False, gold_only: bool = False,
) -> None:
    settings = get_settings()
    repo = Repo(settings.database_url)
    repo.create_all()
    deps = build_deps(settings, repo)

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = resume_path or RESULTS_DIR / f"run_{timestamp}.json"
    state: dict[str, Any] = {"timestamp": timestamp, "questions": [], "baseline": []}
    if resume_path and resume_path.exists():
        state = json.loads(resume_path.read_text(encoding="utf-8"))
        print(f"Resuming {resume_path}: {len(state['questions'])} questions already done.")

    def save() -> None:
        out_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    if planted_only:
        state.pop("planted_claim_recall", None)
    if gold_only:
        gold = await evaluate_gold_labels(deps, repo)
        out = RESULTS_DIR / f"gold_{timestamp}.json"
        out.write_text(json.dumps(gold, indent=2), encoding="utf-8")
        print(f"n={gold['n']} accuracy={gold['accuracy']} kappa={gold['kappa']} "
              f"false_rejection_rate={gold['false_rejection_rate']} -> {out}")
        return
    if "planted_claim_recall" not in state:
        print("Evaluating planted claims (Verifier only, no search provider needed)...")
        planted_results = await evaluate_planted_claims(deps)
        state["planted_claim_recall"] = compute_planted_claim_recall(planted_results)
        state["planted_claim_details"] = planted_results
        by_stage: dict[str, int] = {}
        for r in planted_results:
            if r["rejected"]:
                key = r["stage_failed"] or r["outcome"]
                by_stage[key] = by_stage.get(key, 0) + 1
        state["planted_rejections_by_stage"] = by_stage
        print(f"Planted-claim recall: {state['planted_claim_recall']} by stage: {by_stage}")
        save()

    if planted_only:
        return
    if injection_only or "injection" not in state:
        print("Evaluating prompt-injection resistance (3 fixture pages, real pipeline)...")
        state["injection"] = await evaluate_injection(settings, repo, deps)
        save()
    if injection_only:
        return

    if "gold_label_agreement" not in state:
        print("Evaluating gold labels (independent grader vs. human)...")
        state["gold_label_agreement"] = await evaluate_gold_labels(deps, repo)
        if state["gold_label_agreement"]["n"] == 0:
            print("  gold_labels.csv has no human-labeled rows — skipping (never fabricated).")
        save()

    with open(QUESTIONS_PATH, "r", encoding="utf-8") as fh:
        questions = yaml.safe_load(fh)
    entries = _select_entries(questions, per_domain, limit, include_premise)
    done_ids = {q["id"] for q in state["questions"]}
    todo = [e for e in entries if e["id"] not in done_ids]

    print(f"Running {len(todo)} questions through the full pipeline (spends real credits, concurrency={concurrency})...")
    sem = asyncio.Semaphore(concurrency)

    async def one(entry: dict[str, Any]) -> None:
        async with sem:
            try:
                result = await evaluate_question(settings, repo, entry, deps=deps)
            except Exception as exc:  # a failed question is recorded, never silently dropped
                result = {"id": entry["id"], "error": str(exc)[:300]}
            result["is_premise"] = bool(entry.get("is_premise"))
            state["questions"].append(result)
            print(f"  {entry['id']}: {result.get('stop_reason', 'ERROR')} cost=${result.get('cost_usd')} "
                  f"claims_passed={result.get('n_claims_passed')}/{result.get('n_claims')}")
            if with_baseline and not entry.get("is_premise"):
                try:
                    base = await evaluate_baseline_question(deps, entry)
                except Exception as exc:
                    base = {"id": entry["id"], "error": str(exc)[:300]}
                state["baseline"].append(base)
                print(f"    baseline citation_precision={base.get('citation_precision')}")
            save()

    await asyncio.gather(*(one(e) for e in todo))
    save()
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DeepDive self-evaluation (spends real Mesh/Firecrawl credits).")
    ap.add_argument("limit", nargs="?", type=int, help="only the first N domain questions (legacy positional arg)")
    ap.add_argument("--per-domain", type=int, help="first N questions from each of the 6 domains")
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--no-premise", action="store_true", help="skip the 5 false-premise questions")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--resume", type=Path, help="continue a partially-written results JSON")
    ap.add_argument("--planted-only", action="store_true", help="re-run only the 60 planted claims (cheap)")
    ap.add_argument("--gold-only", action="store_true", help="score only eval/gold_labels.csv (needs human labels)")
    ap.add_argument("--injection-only", action="store_true", help="run only the 3 injection fixtures")
    args = ap.parse_args()
    asyncio.run(
        main(
            per_domain=args.per_domain, limit=args.limit, with_baseline=not args.no_baseline,
            include_premise=not args.no_premise, concurrency=args.concurrency, resume_path=args.resume,
            planted_only=args.planted_only, injection_only=args.injection_only, gold_only=args.gold_only,
        )
    )
