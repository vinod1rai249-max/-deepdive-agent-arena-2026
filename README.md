# 🔎 DeepDive

**A self-verifying research analyst.** Every sentence in the final report is checked against the source it cites — by code, not by asking the model to promise it's telling the truth.

`✅ 100% Mesh API` — every LLM call in this codebase routes through Mesh, enforced by an automated test (`tests/unit/test_architecture.py`), not a claim in this README. &nbsp;·&nbsp; `✅ 103 tests, 0 live credits to verify correctness` &nbsp;·&nbsp; `✅ Real runs against live Mesh + Firecrawl` — not just mocks

Built for **Agent Arena 2026, Track 01**. Full spec: [`BLUEPRINT.md`](BLUEPRINT.md) · Design rationale: [`DESIGN.md`](DESIGN.md) · Self-eval detail: [`EVAL.md`](EVAL.md)

> *(Screenshot slot: drop a capture of the Streamlit Report tab here — `docs/report-tab.png` — showing the grouped, cited report in the UI described below.)*

---

## TL;DR

- **Citations are structural, not prompted.** The model never types a real ID or URL for a citation — it's resolved via a database foreign-key chain. A prompt injection or a confident hallucination can't forge a source; there's no field for it to write one into.
- **Verification is two layers, not one.** An LLM quotes the exact span it's relying on; code then independently confirms that span actually exists in the stored text and that the numbers match. The LLM can lie about entailment — it's much harder to fabricate a verbatim quote that also survives a fuzzy-match against real text it never saw.
- **This isn't a demo that only works on the happy path.** It's been run against live Mesh and live Firecrawl today, found three real bugs doing it, fixed all three, and kept working through an actual mid-run server crash — see [§ Proven against real services](#proven-against-real-services-not-just-mocks).

## The problem this is built against

The brief's own framing: *"Tools that produce cited reports from one question already exist. Their well-known weakness is trust: they cite sources, but the cited source often does not actually support the sentence attached to it."* That's the specific failure mode DeepDive is built to make structurally hard, not just less likely. Not a search wrapper, not a single long prompt — a graph of distinct agents where the grounding guarantee lives in code that runs regardless of what any one model call decides to do.

## At a glance

| Claim | Where to verify it |
|---|---|
| Citations genuinely trace to checked sources | `agents/verifier.py` — span-quote (LLM) → span-exists (code, `rapidfuzz`) → numeric-consistency (code) |
| The model can't author a fake citation | `agents/schemas.py` (index-based citing) + `store/repo.py::get_report` (FK resolution) — DESIGN.md §1, §3 |
| The loop is real, not a retry wrapper | `agents/loop_control.py` — failure reasons feed back into *different* search queries, not the same one again |
| A second, different model audits the first | `llm/models.yaml` — `verifier` and `auditor` roles are different underlying models, by rule |
| Sources that disagree are surfaced, not resolved by fiat | `agents/contradiction.py` — renders both claims verbatim, never picks a winner |
| Self-eval has no fabricated numbers | `EVAL.md` — states plainly what's measured vs. pending, with no padding |

## How the agents cooperate

```
Planner → Researcher (parallel per sub-question) → Evidence Curator → Drafter → Claim Verifier
    ▲                                                                                  │
    └──────────── targeted re-research on thin sub-questions ◄────────────────────────┤ coverage < 0.6
                                                                                        ▼
                             Contradiction Checker → Synthesizer → Report Auditor → Final report
                                                                          ▲
                                                      Human Reviewer ─────┘ (optional; promotes/confirms
                                                                             a claim, triggers scoped re-synthesis)
```

| Agent | Job | Model role | What makes it distinct |
|---|---|---|---|
| **Planner** | Splits the question into 3–7 sub-questions with acceptance criteria; flags a false/loaded premise | `planner` | Only agent that can set `premise_flag` |
| **Researcher** | Generates search queries, fetches real web content (Firecrawl), runs concurrently across sub-questions | `cheap` (query-gen) | Only agent that touches the network |
| **Evidence Curator** | Dedupes near-identical sources, chunks into passages | *none — deterministic code* | The one pipeline stage with zero LLM calls |
| **Drafter** | Extracts atomic claims from passages, one fact per claim | `drafter` | Cites by per-call index only, never a real ID |
| **Claim Verifier** | Independently checks each claim against its cited source | `verifier` | The only agent whose output is cross-checked by code, not just read |
| **Contradiction Checker** | Flags verified claims that genuinely disagree | `cheap` | Never resolves a disagreement — always keeps both |
| **Synthesizer** | Writes report sentences from verified claims only | `synth` | Composed per sub-question, so context never grows unbounded |
| **Report Auditor** | Independently re-checks every synthesized sentence | `auditor` | **Always a different model than `verifier`** — by rule, not convention |
| **Human Reviewer** | Optional: promotes or confirms a rejected claim | *none — a person* | Never mutates the AI's original verdict; additive only |

## Bonus coverage (brief's "Highlighted Bonus" items)

| Item | Status |
|---|---|
| Structured framework (LangGraph) with a real loop-back on verification failure | ✅ `agents/loop_control.py` + a full-graph integration test proves a thin sub-question triggers genuine re-research |
| Contradiction detection across sources | ✅ `agents/contradiction.py`, rendered as "Sources disagree" quoting both claims verbatim |
| Source-quality weighting, visible in code | ✅ `retrieval/tiers.py` (rule-based tiers) feeding `verify/confidence.py` (tier + recency + corroboration) |
| Clean export and follow-up | ⚠️ Partial — JSON export/replay and Markdown export are built and tested (`cli.py export`/`replay`); PDF export and conversational follow-up are **not built** (stated plainly, see `BLUEPRINT.md` §17) |

## Point to a rejected claim

From a real run today (`What are the main risks of investing in the EV battery market in 2026?`, 153 claims drafted, 137 passed verification — 116 verified, 19 re-cited, 2 partial, **16 rejected**):

> **Claim drafted:** *"The EV battery recycling capacity is expected to expand by 2026."*
> **The model's own self-report:** entailment = `supported` — *"the passage explicitly mentions 'EV BATTERY RECYCLING CAPACITY EXPANSION', directly supporting the claim."*
> **What it actually quoted as evidence:** `"5.10.2\n\nEV BATTERY RECYCLING CAPACITY EXPANSION"` — a report's section heading.
> **Verdict: rejected.** `stage_failed=numeric_mismatch` — the claim asserts *2026*; the quoted span is a table-of-contents entry that never says so. The model said "supported." The code checked what was actually quoted, not what the model said about it, and rejected it anyway.

This is the exact failure mode a single-LLM-judge system can't catch — the model confidently cited *something real*, just not something that actually substantiates the specific fact being claimed. The same pattern caught a second claim in the same run (`"The 2026 Iran-US conflict is projected to impact..."` — also quoted a bare section heading). Full list, with the AI's reason alongside, is in the Streamlit UI's **Rejected & Unverified** tab — and from there, a human can promote or confirm any of them (see Human Reviewer above).

## Proven against real services, not just mocks

103 tests pass fully mocked — correctness doesn't depend on a live key. But mocks can't prove a system survives contact with real, imperfect services, so this one has been run against live Mesh and live Firecrawl, not just CI. That surfaced three real issues, found and fixed the same day, not swept into a "known limitations" list:

| Found | Root cause | Fix |
|---|---|---|
| A verifier model 404'd on every call | A placeholder model ID (`claude-3-5-sonnet-20241022`) didn't exist on Mesh at all | Queried Mesh's real `/v1/models` catalog live, confirmed working replacements, corrected pricing to match |
| Verification silently broke entirely on the new model | Claude wraps JSON in markdown fences despite instructions not to; the client's own fallback logic refused to try a backup model on a schema failure | Strip fences before parsing; let schema failures fall through to the next candidate model like any other failure |
| Every sub-question showed 0% coverage in the trail | The loop computed and used real coverage internally, but never wrote it back to the DB | One missing `UPDATE`, now covered by a regression test |

The second bug is the more interesting story: while it was live and unfixed, **real concurrent requests from outside this session hit it**, got caught by the bug, degraded gracefully instead of crashing (by design — every agent-level Mesh failure is absorbed, never fatal), and were left `in_progress` when the server was killed to deploy the fix. On restart, the crash-resume feature (LangGraph's own checkpointer, not a hand-rolled scheme) picked both up and completed them cleanly on the fixed code — an unplanned, real-world validation of graceful degradation *and* resume-after-crash in one accident.

## Self-evaluation — honest status

The brief: *"a team that genuinely built this almost always measured whether it works."* Here's the honest version: the **measurement infrastructure is fully built and unit-tested** — 30 authored questions across 6 domains with written rubrics, 60 planted bad claims across 4 failure types, 3 prompt-injection fixtures, an independent `grader` model role (distinct from `verifier`/`auditor`) for judge-human agreement, and a baseline comparator that's actually scored by the same standard as DeepDive, not just generated. **The headline numbers are not yet produced** — that needs a real `MESH_API_KEY` run plus human-labeled `gold_labels.csv` (owned by a person, not fabricated by an LLM). `EVAL.md` says exactly this, with nothing papered over. Real, live run data already exists today (see above) — the gap is specifically the structured eval sweep across all 30+5 questions.

## Quickstart

```bash
pip install -r requirements.txt
# paste a real MESH_API_KEY (and ideally FIRECRAWL_API_KEY) into .env
python cli.py init-db
pytest -q                                            # 103 tests, fully mocked
python cli.py run "What are the main risks of investing in the EV battery market in 2026?"
streamlit run ui/streamlit_app.py                    # or: uvicorn app.api:app --reload
```

## What we deliberately didn't build, and why

| Skipped | Why |
|---|---|
| Vector DB for retrieval | Keeps Mesh as the only AI dependency; passages are chunked, not embedded — `DESIGN.md` §9 |
| Alembic migrations | No live production data to migrate across schema versions in a hackathon build — `DESIGN.md` §8 |
| A pause-for-confirmation when the Planner flags a false premise | Would make the automated "abstention accuracy" eval metric depend on a human being present for every run — actively counterproductive, not just unnecessary — `DESIGN.md` §15 |
| Docker, Prometheus, Langfuse, API auth | Real engineering practice, zero weight in what this track actually judges — `BLUEPRINT.md` §17 |

## The docs, and why they're separate

This README is the pitch — skim it, verify the claims, move on. `BLUEPRINT.md` is the full engineering spec (what's built, phase by phase). `DESIGN.md` is the rationale (why, and what was rejected). `EVAL.md` is the measurement detail. Four documents with four different jobs, not one file trying to be all of them at once.
