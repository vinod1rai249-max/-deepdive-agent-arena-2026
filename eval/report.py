"""Formats an eval/results/*.json file into the markdown table EVAL.md
uses (BLUEPRINT.md §15). Pure formatting — no live execution, no
credits. Usage: `python eval/report.py eval/results/run_<ts>.json`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

from eval.run_eval import bootstrap_ci


def _pct(values: list[float]) -> str:
    """Mean with a bootstrap 95% CI, e.g. `0.912 [0.870, 0.950] (n=12)`."""
    if not values:
        return "n/a"
    lo, hi = bootstrap_ci(values)
    return f"{mean(values):.3f} [{lo:.3f}, {hi:.3f}] (n={len(values)})"


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def render(results: dict[str, Any]) -> str:
    planted = results.get("planted_claim_recall", {})
    questions = [q for q in results.get("questions", []) if "error" not in q]
    errored = [q for q in results.get("questions", []) if "error" in q]
    research = [q for q in questions if not q.get("is_premise")]
    premise = [q for q in questions if q.get("is_premise")]

    lines = [
        f"## Self-evaluation results — {results.get('timestamp', 'unknown run')}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Planted-claim recall (overall) | {planted.get('overall')} |",
    ]
    for claim_type, recall in sorted(planted.get("by_type", {}).items()):
        lines.append(f"| Planted-claim recall — {claim_type} | {recall} |")
    if results.get("planted_rejections_by_stage"):
        lines.append(f"| Planted-claim rejections by stage | {results['planted_rejections_by_stage']} |")

    inj = results.get("injection")
    if inj:
        lines.append(f"| Injection resistance (runs where the attack was followed, target 0) | {inj['followed']}/{inj['n']} |")

    gold = results.get("gold_label_agreement")
    if gold and gold.get("n"):
        lines.append(f"| Judge–human agreement (accuracy, n={gold['n']}) | {gold.get('accuracy')} |")
        lines.append(f"| Judge–human agreement (Cohen's κ, n={gold['n']}) | {gold.get('kappa')} |")
        if gold.get("false_rejection_rate") is not None:
            lines.append(f"| False-rejection rate | {gold['false_rejection_rate']} |")
    else:
        lines.append("| Judge–human agreement / false-rejection rate | not measured — `gold_labels.csv` has no human labels |")

    if questions:
        lines.append(f"| Questions evaluated (research / false-premise) | {len(research)} / {len(premise)} |")
        if errored:
            lines.append(f"| Questions that errored (excluded) | {len(errored)}: {[q['id'] for q in errored]} |")

    if research:
        lines.append(f"| Claim pass rate (verified+partial+re-cited / drafted) | {_pct([q['claim_pass_rate'] for q in research if q.get('claim_pass_rate') is not None])} |")
        lines.append(f"| Sentence support rate (Auditor kept / synthesized cited) | {_pct([q['sentence_support_rate'] for q in research if q.get('sentence_support_rate') is not None])} |")
        lines.append(f"| Citation precision, by construction (regression guard) | {_pct([q['citation_precision'] for q in research if q.get('citation_precision') is not None])} |")

        graded = [q["independent_grade"] for q in research if q.get("independent_grade")]
        n_sent = sum(g["n_cited_sentences"] for g in graded)
        if n_sent:
            strict = sum(round(g["strict_support_rate"] * g["n_cited_sentences"]) for g in graded if g["strict_support_rate"] is not None)
            lenient = sum(round(g["lenient_support_rate"] * g["n_cited_sentences"]) for g in graded if g["lenient_support_rate"] is not None)
            lines.append(f"| Independent-grader support, strict ('supported') — pooled | {strict / n_sent:.3f} ({strict}/{n_sent} sentences) |")
            lines.append(f"| Independent-grader support, lenient (+'partial') — pooled | {lenient / n_sent:.3f} ({lenient}/{n_sent} sentences) |")
            lines.append(f"| Grader errors (counted as unsupported) | {sum(g['grader_errors'] for g in graded)} |")

        rubric = [q["rubric_coverage"] for q in research if q.get("rubric_coverage") is not None]
        if rubric:
            lines.append(f"| Rubric coverage (grader judges final report vs pre-written rubric) | {_pct(rubric)} |")
        lines.append(f"| Gap / unverified notes per report (mean) | {mean(q.get('n_gap_notes', 0) for q in research):.1f} |")

    if premise:
        flagged = sum(1 for q in premise if q.get("premise_flagged"))
        lines.append(f"| Abstention accuracy (false premise flagged by Planner) | {flagged}/{len(premise)} |")

    if questions:
        costs = [q["cost_usd"] for q in questions if q.get("cost_usd") is not None]
        lat = [q["latency_s"] for q in questions if q.get("latency_s") is not None]
        if costs:
            lines.append(f"| Cost per question, pipeline only (est.) mean / p50 / p95 | ${mean(costs):.3f} / ${median(costs):.3f} / ${_quantile(costs, 0.95):.3f} |")
        if lat:
            lines.append(f"| Latency per question mean / p50 / p95 (s) | {mean(lat):.0f} / {median(lat):.0f} / {_quantile(lat, 0.95):.0f} |")
        stop_reasons: dict[str, int] = {}
        for q in questions:
            stop_reasons[q.get("stop_reason") or "unknown"] = stop_reasons.get(q.get("stop_reason") or "unknown", 0) + 1
        lines.append(f"| Stop-reason distribution | {stop_reasons} |")

    baseline = [b for b in results.get("baseline", []) if "error" not in b]
    if baseline:
        n_total = sum(b["n_sentences"] for b in baseline)
        supported = sum(round(b["citation_precision"] * b["n_sentences"]) for b in baseline if b.get("citation_precision") is not None)
        unfetchable = sum(1 for b in baseline for d in b.get("details", []) if not d.get("url_fetchable"))
        if n_total:
            lines.append(f"| Baseline citation precision, same grading standard — pooled | {supported / n_total:.3f} ({supported}/{n_total} sentences) |")
            lines.append(f"| Baseline citations whose URL could not be fetched | {unfetchable}/{n_total} |")

    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m eval.report <results.json>")
        raise SystemExit(1)
    path = Path(sys.argv[1])
    with open(path, "r", encoding="utf-8") as fh:
        results = json.load(fh)
    print(render(results))


if __name__ == "__main__":
    main()
