"""Formats an eval/results/*.json file into the markdown table EVAL.md
uses (BLUEPRINT.md §15). Pure formatting — no live execution, no
credits. Usage: `python eval/report.py eval/results/run_<ts>.json`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any


def render(results: dict[str, Any]) -> str:
    planted = results.get("planted_claim_recall", {})
    questions = results.get("questions", [])

    lines = [
        f"## Self-evaluation results — {results.get('timestamp', 'unknown run')}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Planted-claim recall (overall) | {planted.get('overall')} |",
    ]
    for claim_type, recall in sorted(planted.get("by_type", {}).items()):
        lines.append(f"| Planted-claim recall — {claim_type} | {recall} |")

    gold = results.get("gold_label_agreement")
    if gold and gold.get("n"):
        lines.append(f"| Judge–human agreement (accuracy, n={gold['n']}) | {gold.get('accuracy')} |")
        lines.append(f"| Judge–human agreement (Cohen's κ, n={gold['n']}) | {gold.get('kappa')} |")
        if gold.get("false_rejection_rate") is not None:
            lines.append(f"| False-rejection rate | {gold['false_rejection_rate']} |")

    if questions:
        costs = [q["cost_usd"] for q in questions if q.get("cost_usd") is not None]
        precisions = [q["citation_precision"] for q in questions if q.get("citation_precision") is not None]
        stop_reasons: dict[str, int] = {}
        for q in questions:
            stop_reasons[q.get("stop_reason") or "unknown"] = stop_reasons.get(q.get("stop_reason") or "unknown", 0) + 1

        lines.append(f"| Questions evaluated | {len(questions)} |")
        if costs:
            lines.append(f"| Mean cost per question (est.) | ${mean(costs):.4f} |")
        if precisions:
            lines.append(f"| Mean citation precision (DeepDive) | {mean(precisions):.4f} |")
        lines.append(f"| Stop-reason distribution | {stop_reasons} |")

    baseline = results.get("baseline", [])
    baseline_precisions = [b["citation_precision"] for b in baseline if b.get("citation_precision") is not None]
    if baseline_precisions:
        lines.append(f"| Mean citation precision (baseline, same grading standard) | {mean(baseline_precisions):.4f} |")

    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python eval/report.py <results.json>")
        raise SystemExit(1)
    path = Path(sys.argv[1])
    with open(path, "r", encoding="utf-8") as fh:
        results = json.load(fh)
    print(render(results))


if __name__ == "__main__":
    main()
