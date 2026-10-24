"""Independent re-grade of runs ALREADY stored in the database (Mesh only —
no Firecrawl, no new research). Every cited sentence in each run's final
report is re-judged by the `grader` role (neither the Verifier nor the
Auditor model) against the stored passage behind its citation.

Use when fresh pipeline runs aren't possible (e.g. Firecrawl credits are
exhausted) or to grade runs produced earlier. Honest scope: it measures the
reports as they were generated — under whatever model config was live then.

    python -m eval.grade_runs                 # every complete run in the DB
    python -m eval.grade_runs <run_id> ...    # specific runs
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import yaml

from app.runner import build_deps
from core.config import get_settings
from eval.run_eval import QUESTIONS_PATH, RESULTS_DIR, grade_final_report, grade_rubric
from store.models import Run
from store.repo import Repo


def _rubric_entry(question: str) -> dict | None:
    """The pre-written rubric, if this run's question is one of questions.yaml's."""
    data = yaml.safe_load(open(QUESTIONS_PATH, encoding="utf-8"))
    entries = [q for d in data["domains"].values() for q in d] + data.get("false_premise", [])
    return next((q for q in entries if q["question"].strip() == question.strip()), None)


async def main(run_ids: list[str]) -> None:
    settings = get_settings()
    repo = Repo(settings.database_url)
    deps = build_deps(settings, repo)
    if not run_ids:
        with repo.session() as session:
            run_ids = [r.id for r in session.query(Run).filter_by(status="complete").all()]

    out: list[dict] = []
    for run_id in run_ids:
        export = repo.export_run(run_id)
        question = export["run"]["question"]
        grade = await grade_final_report(deps, export)
        entry = _rubric_entry(question)
        rubric = await grade_rubric(deps, entry, export) if entry and "rubric" in entry else None

        statuses = [c["status"] for c in export["claims"]]
        passed = sum(1 for s in statuses if s in ("verified", "partial", "re_cited"))
        verifier_models = sorted({c["model"] for c in export["llm_calls"] if c["node"] == "verifier" and not c["error"]})
        row = {
            "run_id": run_id, "question": question, "stop_reason": export["run"]["stop_reason"],
            "n_claims": len(statuses), "n_claims_passed": passed,
            "claim_pass_rate": round(passed / len(statuses), 4) if statuses else None,
            "independent_grade": grade, "rubric_coverage": rubric,
            "verifier_models_used": verifier_models,
        }
        out.append(row)
        print(f"{question[:60]!r}: claims {passed}/{len(statuses)}, strict={grade['strict_support_rate']} "
              f"lenient={grade['lenient_support_rate']} (n={grade['n_cited_sentences']}) rubric={rubric}")

    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"grade_existing_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"runs": out}, indent=2), encoding="utf-8")
    print(f"written {path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
