# DeepDive — Self-Evaluation

This is the brief's own emphasis: "a team that genuinely built this almost always measured whether it works." This document is honest about what's measured, what's built but not yet run for real, and exactly what's still needed to produce the headline numbers.

## Status

**The measurement infrastructure is built and tested. The headline numbers are not yet produced**, because producing them for real requires two things only the user can provide:

1. A real `MESH_API_KEY` — `eval/run_eval.py main()` spends real credits running every question in `eval/questions.yaml` through the full pipeline and every planted claim in `eval/planted_claims.yaml` through the Claim Verifier.
2. A filled-in `eval/gold_labels.csv` — `eval/label.py` is a working CLI for collecting these interactively from a real run's claim–passage pairs, but no one has run it yet.

Nothing in this repository fabricates a result to fill this gap. `eval/results/` is empty. This table will be filled in with real output from `python eval/report.py eval/results/run_<timestamp>.json` once both of the above exist.

## What's built and verified (without spending credits)

| Component | What it does | Verified by |
|---|---|---|
| `eval/questions.yaml` | 30 questions across 6 domains (finance, health, tech, energy/climate, policy, history), each with a rubric written before any run, plus 5 dedicated false-premise questions | Content review; loaded and iterated correctly by `run_eval.py` |
| `eval/planted_claims.yaml` | 60 injected bad claims (15 each: wrong_number, wrong_entity, unsupported_extrapolation, contradiction), each paired with a representative passage | `tests/integration/test_eval_planted_claims.py` — all 60 load and route through the real `agents.verifier.verify_all` correctly |
| `eval/injection_pages/` | 3 HTML fixtures with embedded prompt-injection attempts, targeting the Researcher/Drafter, the Synthesizer, and the Auditor specifically | Content review against the `<untrusted_source>` wrapping rule (BLUEPRINT.md §8) |
| `eval/label.py` | Interactive CLI pulling claim–passage pairs from a real run (via `Repo.export_run`) for human labeling | Code review; depends on a real run existing |
| `eval/run_eval.py` | Orchestrates planted-claim, gold-label, question-based, and baseline evaluation; all metric-calculation functions (`compute_planted_claim_recall`, `compute_sentence_support_rate`, `compute_citation_precision`, `compute_rubric_coverage`, `bootstrap_ci`, `cohen_kappa`) | `tests/unit/test_eval_metrics.py` — each function tested against constructed data, independent of live execution |
| `eval/run_eval.py::evaluate_gold_labels` | Runs the independent `grader` role (a model distinct from `verifier`/`auditor`) against every row in `gold_labels.csv`, scores accuracy + Cohen's κ against the human label, and computes false-rejection-rate against the real `Claim` rows those gold claim_ids came from | `tests/unit/test_eval_grader.py` — accuracy/κ against a mocked grader, false-rejection-rate against seeded `Claim` rows; on today's empty `gold_labels.csv` it correctly returns `n=0` rather than fabricating a number |
| `eval/baseline.py` | Single-prompt "answer with citations" comparator — no retrieval, no verification, LLM-authored URLs — the exact failure mode the brief calls out | Code review; orchestration not yet run live |
| `eval/baseline.py::score_baseline_answer` | Actually fetches each of the baseline's LLM-authored citation URLs and grades them with the exact same span-existence + numeric-consistency check the Report Auditor applies to DeepDive's own sentences — same grading standard both sides, so the comparison means something | `tests/integration/test_baseline_scoring.py` — a genuinely-supported citation, an unfetchable URL, and an SSRF-blocked URL all score correctly |
| `eval/report.py` | Formats a results JSON (including gold-label agreement and the baseline comparison) into this document's table format | `tests/unit/test_eval_metrics.py::test_report_render_*` |

## The metrics this will report (BLUEPRINT.md §15)

| Metric | Definition | Status |
|---|---|---|
| Sentence support rate | Final sentences the Report Auditor judged supported / total drafted | Computed, not yet run live |
| Citation precision | Citations in the final report whose claim has a passing verdict / total citations | Computed, not yet run live (expected ≈1.0 by construction — the Synthesizer only ever sees verified claims; this metric exists to catch a regression, not because a lower number is expected) |
| Planted-claim recall | Planted bad claims rejected / planted, overall and per type | Orchestration tested against a mocked "perfect verifier"; real recall against a real model is pending |
| False-rejection rate | Gold-supported claims the Verifier rejected / gold-supported | `evaluate_gold_labels()` computes this against real `Claim` rows, tested; **blocked on `gold_labels.csv` having real rows** |
| Judge–human agreement | Accuracy + Cohen's κ, grader vs. `gold_labels.csv` | **Fully wired** (`evaluate_gold_labels()` + the independent `grader` model role) and tested against mocked data; **blocked on `gold_labels.csv` having real rows**, same as above |
| Rubric coverage | Rubric aspects covered by verified claims / total aspects | `compute_rubric_coverage()` implemented and tested; not yet run live |
| Abstention accuracy | False-premise questions correctly flagged / 5 | Pending a live run over `eval/questions.yaml`'s `false_premise` section |
| Injection resistance | Runs where injected instructions were followed (target 0) | `injection_pages/` fixtures built; not yet run |
| Loop lift | Mean coverage after the loop − before | Pending a live run (needs `coverage` at iteration 0 vs. final — both already recorded per-run in `Run.iterations`/the trail) |
| Cost / latency | Mean USD, p50/p95 seconds per report | `LLMCall.cost_usd` now computed from `llm/pricing.yaml` (previously hardcoded to 0.0 — fixed during Phase 5); pending a live run for real numbers |

## Comparisons (BLUEPRINT.md §15)

- **Baseline** (`eval/baseline.py`): built and **scored** — `score_baseline_answer()` fetches each of its citation URLs for real and grades them with the same standard the Report Auditor holds DeepDive to, so the comparison is apples-to-apples. `eval/run_eval.py main()` runs it alongside every question by default. Not yet run live.
- **Ablations** (−span check, −numeric check, −re-attribution, −Report Auditor, −loop): not yet built. Each is a small, well-isolated change (e.g., skip stage 2 in `agents/verifier.py`) — straightforward to add once there's a live key to measure against, but not done speculatively without the ability to verify the ablation itself runs correctly.

## How to actually produce this table

```bash
# 1. Add a real key to .env
# MESH_API_KEY=rsk_...
# FIRECRAWL_API_KEY=...   (optional but recommended — without it, every
#                           question falls back to NullSearchProvider and
#                           every sub-question reports "insufficient evidence")

# 2. Run a handful of real questions, then label gold data from them
python cli.py run "What are the main risks of investing in the EV battery market in 2026?"
python eval/label.py <run_id> --target 100

# 3. Run the full eval (spends credits — ~35 questions + 60 planted claims
#    + gold-label grading + a baseline run per question by default)
python eval/run_eval.py
python eval/run_eval.py --no-baseline   # skip the baseline comparison to cut cost roughly in half
python eval/run_eval.py 5                # limit to the first 5 questions, e.g. for a quick smoke run

# 4. Render the table
python eval/report.py eval/results/run_<timestamp>.json
```

## Failure gallery

Pending a live run — BLUEPRINT.md §15 asks for 5 real failures with analysis, and this document does not fabricate those either.
