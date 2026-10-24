# DeepDive — Implementation Blueprint (Final, Reviewed)

> **For Claude Code.** Authoritative spec for DeepDive (Agent Arena 2026, Track 01).
> Work the **Core path** (§17) in order. Each phase ends with: tests green, `python -m compileall .` green, a short report (done / verified / not verified), and a commit. Stop for review between phases.
> This version supersedes the original draft blueprint. Changes from that draft are called out inline as **[REVISED]**; rationale for each lives in `DESIGN.md`.

> **SCOPE FROZEN — 2026-10-24 (submission 2026-10-31).** No new features. Allowed: bug fixes, correcting docs/numbers that turn out to be wrong, and human labelling of `eval/gold_labels.csv`. Out of scope and not to be added: PDF export, uploaded documents, ablations, a Firecrawl-backed re-run, Docker/auth/metrics, further tuning of `verify/scope_check.py` or `verify/gap_reason.py`. See DESIGN.md §20 and the README "Scope freeze".

---

## 0. Mission

Build **DeepDive**: a multi-agent research system that answers a question with a structured, cited report in which **every sentence is verified against the source it cites**. Unsupported claims are re-cited, flagged, or removed; thin areas trigger more research; every decision is logged and replayable; the system measures its own groundedness; and it keeps working — gracefully, not silently — when a dependency fails.

Judging emphasis (from the brief): distinct agents with real orchestration, a verifier that demonstrably rejects claims, citations that genuinely support sentences, a visible decision trail, honest self-measured numbers. **Optimize for those first.** Production hardening (Docker, metrics, auth) is explicitly secondary — see the Hardening bucket in §17.

---

## 1. Non-negotiables (submission validity)

1. **Every LLM/AI call routes through Mesh API.** OpenAI SDK, `base_url="https://api.meshapi.ai/v1"`, key from env `MESH_API_KEY` (prefix `rsk_`).
   - No local models of any kind (no Ollama, llama.cpp, transformers, sentence-transformers, local NLI, local embeddings). Embeddings, if ever used, also go through Mesh.
   - Third-party tools must not make AI calls on our behalf (Firecrawl LLM-extract / JSON mode are **forbidden** — search + scrape only).
   - `llm/client.py` is the **only** module allowed to import `openai`. Enforced by a test that greps the codebase.
2. **[REVISED] Structured-output invariant, zero exceptions**: every single Mesh call goes through one function, `complete_json(node, prompt_name, variables, schema: type[BaseModel], model_role)`. There is no freeform `chat.completions.create` call anywhere in the codebase — not for the planner, not for a one-off date extraction, not for the eval grader. Enforce with a test that greps for any direct `.chat.completions.create(` outside `llm/client.py` and fails the build if found.
3. Repo requirements: `requirements.txt` listing FastAPI and `openai`; `README.md`; `.gitignore` containing `.env`; no committed secrets.
4. CI: download `https://careerapi-production.krishnaik.in/api/ci/hackathons/agent-arena-2026/workflow.yml` to `.github/workflows/agent-arena-2026-checks.yml`. Secrets `MESH_API_KEY` and `SUBMISSION_TOKEN` set by the user in GitHub — never hardcoded.
5. All Python files must compile (critical CI check). Keep `python -m compileall .` green at every commit.

---

## 2. Product contract (put this in the README)

1. Every sentence in the final report is bound to ≥1 source span that **code confirmed exists** in the stored source text and an **independent judge confirmed entails** the sentence.
2. Nothing reaches the report as fact without a passing verdict. Unsupported material is dropped, re-cited, or listed under "Unverified."
3. When evidence is thin, the report says so. It never pads.
4. **[REVISED] Citations are never LLM-authored strings.** The renderer resolves every citation by following `Claim.passage_id → Passage.source_id → Source.url` — a database foreign-key chain, not text the model typed. This is what stops "right fact, hallucinated or altered link."
5. Every decision (sub-questions, searches, sources, claims, verdicts, loop iterations, stop reason, LLM calls, failures) is stored and replayable.
6. **[REVISED] A run degrades gracefully, never catastrophically.** One unreachable source, one failed sub-question, one rate-limited provider, or one process restart should never lose the whole run — see §10.

---

## 3. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| API | FastAPI + Uvicorn |
| Orchestration | LangGraph (`StateGraph`, explicit nodes, conditional edges for the loop) + `langgraph-checkpoint-sqlite` for crash-resume (**added in Phase 5**, not in the original stack list — see DESIGN.md §14) |
| LLM | `openai` SDK → Mesh (sole AI path) |
| Search/scrape | Firecrawl (`firecrawl-py`) — search + scrape only, behind a `SearchProvider` interface so it can be swapped. **[REVISED]** interface must support a second provider as a fallback (see §10) even if only stubbed at first. |
| Uploaded docs | `markitdown` (PDF/DOCX/PPTX → markdown) |
| Validation | Pydantic v2 |
| Storage | SQLite (WAL) via SQLAlchemy 2.x. **[REVISED]** `create_all` for schema creation — no Alembic in the Core path (no live data to migrate in a hackathon build; add Alembic only in the Hardening bucket if the schema needs to survive across deployments). |
| Retry | `tenacity` |
| Fuzzy match | `rapidfuzz` |
| Logging | plain structured dict-logging in Core path; upgrade to `structlog` in Hardening if time remains |
| Tracing | Langfuse — Hardening bucket only |
| UI | Streamlit |
| Tests | `pytest`, `pytest-asyncio`, `respx` (mock Mesh), `pytest-cov` |
| Packaging | Dockerfile/compose — Hardening bucket only |

Do **not** add: vector DBs for web content, queues, microservices, LiteLLM, NeMo Guardrails, extra agent frameworks. **[REVISED, explicit deviation]** uploaded documents are also *not* put in a vector DB — they're chunked into `Passage`s the same way as web content (see `DESIGN.md` §"Deviations" for the tradeoff). If a dependency outside this list seems needed, stop and ask first.

---

## 4. Architecture

```
POST /research ──► job (background task, resumable — §10) ──► LangGraph run
                                              │
 Planner ─► Researcher×N (parallel) ─► Evidence Curator ─► Drafter ─► Claim Verifier
    ▲                                                                      │
    └──── targeted re-research (weak sub-questions) ◄────────────────────┤ coverage < τ AND iter < max AND budget OK
                                                                          ▼
                         Contradiction Checker ─► Synthesizer ─► Report Auditor ─► Final report
                                                                    │ any sentence fails
                                                                    └─► rewrite/strip, re-audit (max 2)
```

Every node: reads typed state, writes typed state, persists its outputs to SQLite **before** moving on (this is what makes resume possible — §10), emits a progress event, fails soft (records failure, does not crash the run).

### 4.1 Agents

| Agent | Input | Output | Notes |
|---|---|---|---|
| **Planner** | question, optional uploaded docs | 3–7 `SubQuestion`s with `acceptance_criteria`, `time_scope` | Detects false/loaded premises → adds a premise-check sub-question, sets `premise_flag`. |
| **Researcher** | one `SubQuestion` (+ failure reasons on re-research) | `Source`s with normalized text, url, content_hash, published_at, tier | **Runs concurrently across sub-questions** (`asyncio.gather` + semaphore — §11). Query generation goes through `complete_json` with an explicit `QueryPlan` schema **[REVISED — was implied, not schema'd]**. Fetch via `fetch_safe`. |
| **Evidence Curator** | sources | deduped sources, `Passage`s with char offsets | Deterministic code, no LLM: near-duplicate merge (shingle/MinHash Jaccard ≥ 0.8), passage chunking (~800 chars, sentence-aligned). |
| **Drafter** | sub-question + passages | atomic `Claim`s, each with `passage_id` | One fact per claim. Must not use knowledge outside passages. |
| **Claim Verifier** | claim + cited passage (+ sibling passages) | `Verdict` | Pipeline in §5, including a cheap pre-filter and batched calls — §11. |
| **Contradiction Checker** | verified claims per sub-question | `Contradiction`s | Pairs claims on the same aspect; records both sources; never silently picks one. |
| **Synthesizer** | verified claims, contradictions, gaps | `Report` where each sentence lists `claim_ids` | Writes only from verified claims. Must include "Gaps / Unverified" and "Sources disagree" sections when applicable. |
| **Report Auditor** | final report sentences + their cited spans | per-sentence `audit_passed`/`audit_reason` | Uses a **different model** than the Claim Verifier. Failing sentences → rewrite from claims or strip; re-audit; max 2 rounds. |
| **Follow-up** | run_id + question | answer | Answers only from stored evidence; new claims go through the same verifier. Hardening-bucket feature. |
| **Human Reviewer** [REVISED — new, §4.2] | a run's rejected/unverified/partial claims | status override + reason, recorded separately from the AI's `Verdict` | Not an LLM agent — a human, acting through the UI or API. Optional on every run; the fully-autonomous path works identically with zero overrides. |

---

## 4.2 Human-in-the-loop override [REVISED — new section]

**Not required by the brief** — this track asks for a *self-verifying* system, and the audit-trail requirement ("a human can see exactly what was kept and what was rejected") is satisfied by the trail alone, which is read *after* a run completes, not approved *during* one. This feature is additive: every run still completes fully autonomously with zero human involvement; this gives a human the option to correct a specific Verifier judgment they disagree with, for the one thing a human can see that the pipeline structurally can't — their own read of a borderline case.

**Why this one, and not other HITL points considered:** a pre-publish approval gate, a premise-flag confirmation pause, and a judge/human disagreement feedback loop were all considered and rejected — the first two add friction without adding signal the pipeline doesn't already have (and the premise-flag pause actively conflicts with the automated abstention-accuracy eval metric, §15), and the third is genuine active-learning scope beyond this track. A low-confidence claim review queue is the one place a human adds judgment the system cannot: whether a Verifier rejection (a strict span-match near-miss, an overly literal numeric check, a re-attribution that didn't find the right sibling passage) was actually a mistake.

**Mechanics:**
- A human reviews a claim with status `rejected`, `unverified`, or `partial` (surfaced in the Streamlit "Rejected & Unverified" tab, alongside the AI's original span/entailment/reason) and either **promotes** it to `verified` with a required reason, or **confirms** the AI's verdict with a required reason (itself a useful signal: "a human looked at this and agreed").
- The override is a separate, additive `HumanReview` row (`claim_id`, `previous_status`, `new_status`, `reason`, `reviewed_at`) — it **never mutates the original `Verdict` row**. Both the AI's original reasoning and the human's decision stay visible side by side; nothing is silently overwritten (BLUEPRINT.md §2.5 principle, applied here too).
- An override triggers `app/runner.py::resynthesize_run`, which re-runs **only** Contradiction Checker → Synthesizer → Report Auditor using the current claim statuses — not retrieval, not drafting, not the Claim Verifier. A promoted claim can now (a) appear in the report and (b) newly contradict an already-verified claim, so both of those stages have to re-run; nothing upstream of them changed, so neither should they.
- `POST /runs/{run_id}/claims/{claim_id}/override {new_status, reason}` is the API surface; the UI calls the same path.
- Spends real Mesh calls (contradiction + synth + audit roles) — this is an explicit, human-triggered action with its own cost, not part of the automatic per-run budget.

---

## 5. Verification pipeline (core — highest priority)

For each claim, stages run in order; first failure decides unless re-attribution succeeds.

0. **[REVISED] Cheap pre-filter (code, free)**: `core/relevance.py.passes_prefilter(claim, passage)` — deterministic keyword/number-overlap check between claim and candidate passage. Pairs with near-zero overlap are rejected before spending any LLM call — this is both a correctness guard and the main cost optimization (§11).
1. **Span quote (LLM)**: judge returns `evidence_span` (verbatim) + `entailment` ∈ {`supported`, `partial`, `unsupported`, `contradicted`} + `reason`.
2. **Span existence (code)** `verify/span_match.py`: NFKC normalize, collapse whitespace, unify quotes/dashes, lowercase; `rapidfuzz.fuzz.partial_ratio(span, passage) >= SPAN_THRESHOLD` (default 92, tuned on gold set, reported in `EVAL.md`). Fail → `stage_failed="span_missing"`.
3. **Entailment**: must be `supported` (or `partial` → claim marked partial, lower confidence, rendered with a qualifier).
4. **Numeric/entity consistency (code)** `verify/numeric_check.py`: numbers, percentages, currency, years/dates, capitalized entities extracted from the claim must appear in the span after normalization. Fail → `stage_failed="numeric_mismatch"`.
5. **Re-attribution (code + LLM)** `verify/reattribute.py`: on stage 2–4 failure, try top-k other passages for the same sub-question (ranked via `core/relevance.py.rank()` — same scorer as stage 0). Pass → re-cite, `outcome="re_cited"`. Else `outcome="rejected"` or `"unverified"`.
5b. **Scope & self-reported-gap checks (code)** `verify/scope_check.py`, `verify/gap_reason.py` `[ADDED after evaluation — DESIGN.md §18, §20]`: a claim may not use a scope-widening word (overall, entire, national, …) absent from the cited passage (`stage_failed="scope_widening"`), and a `partial` verdict whose own reason says the evidence "does not mention/state/link …" fails (`"self_reported_gap"`). Also applied by the Auditor and the follow-up agent.
6. **Confidence (code)** `verify/confidence.py`: f(verdict, source tier, count of independent supporting sources, source age vs `time_scope`). Logic readable in code, not a prompt.

Every claim writes a `verdicts` row: `stage_reached`, `stage_failed`, `outcome`, `reason`, `evidence_span`, `passage_id`, `model`, `prompt_version`, `latency_ms`.

The **Report Auditor** applies stages 1–4 to each final sentence against the spans of its `claim_ids`.

---

## 6. Loop engineering

Graph state: `iteration`, `max_iterations=3`, `budget_usd`, `budget_tokens`, `deadline_s`, `coverage: dict[subq_id, float]`, `stop_reason`.

- Coverage per sub-question = fraction of its acceptance criteria satisfied by ≥1 verified claim, judged via a cheap-model Mesh call returning a **`CoverageJudgment` schema** `[REVISED — previously unschemaed]` — weighted down if only low-tier or stale sources.
- Loop back to Researcher **only** for sub-questions with coverage < `COVERAGE_THRESHOLD` (default 0.6), passing structured failure reasons (`missing_aspect`, `low_tier_only`, `stale_only`, `all_rejected`).
- **No-new-evidence detector**: if re-research yields only already-seen content hashes, stop with `no_new_evidence`.
- Stop reasons stored on the run: `coverage_met | max_iter | budget | deadline | no_new_evidence`.
- All limits enforced in code (`core/budget.py`), never in prompts, and **checked before each expensive call, not only post-hoc** `[REVISED]` — a run must not blow through its budget mid-call.

---

## 7. Data models (Pydantic — `app/state.py`, `store/models.py`)

```python
class SubQuestion:   id, run_id, text, acceptance_criteria: list[str], time_scope: str|None, premise_flag: bool, coverage: float
class Source:        id, run_id, subq_id, url, domain, title, published_at: date|None, tier: Literal[1,2,3,4], content_hash, text, fetched_at, origin: Literal["web","upload"]
class Passage:       id, source_id, start, end, text
class Claim:         id, run_id, subq_id, text, passage_id, status: Literal["pending","verified","partial","re_cited","rejected","unverified"]
class Verdict:       id, claim_id, stage_reached, stage_failed: str|None, outcome, entailment, evidence_span, reason, passage_id, model, prompt_version, latency_ms
class Contradiction: id, run_id, subq_id, claim_a, claim_b, explanation
class ReportSentence:id, run_id, section, order, text, claim_ids: list[str], audit_passed: bool, audit_reason: str|None
class LLMCall:       id, run_id, node, model, prompt_version, input_tokens, output_tokens, cost_usd, latency_ms, cached: bool, error: str|None
class Run:           id, question, status: Literal["pending","in_progress","complete","failed"], created_at, finished_at, stop_reason, iterations, total_cost_usd, config_snapshot: dict, last_completed_node: str|None
class Search:        id, run_id, subq_id, iteration, query, provider, result_count
class HumanReview:   id, run_id, claim_id, previous_status, new_status, reason, reviewed_at  # [REVISED — §4.2]
```

`Run.last_completed_node` **[REVISED — new field]** is what makes resume (§10) possible: on restart, find runs with `status="in_progress"` and re-enter the graph after that node instead of from scratch.

Source tiers (`retrieval/tiers.py`, rule-based, documented): 1 = government/regulators/standards bodies/peer-reviewed/company primary filings; 2 = major news/wire/established research firms; 3 = trade publications/reputable blogs; 4 = forums/unknown blogs/content farms.

---

## 8. LLM gateway (`llm/client.py`)

- Single `MeshClient` wrapping `OpenAI(base_url="https://api.meshapi.ai/v1", api_key=settings.mesh_api_key)`.
- `complete_json(node, prompt_name, variables, schema: type[BaseModel], model_role) -> BaseModel` — the **only** entry point; see §1.2.
  - Model selected from `llm/models.yaml` by role (`planner`, `drafter`, `verifier`, `auditor`, `synth`, `grader`, `coverage`, `cheap`). **Verifier, auditor and grader must share NO model, primary or fallback (tests/unit/test_model_roles.py).** List available models from Mesh (or ask the user) before picking IDs; record chosen IDs in `models.yaml`.
  - `tenacity` retry on 429/5xx/timeouts (exponential, max 4) **+ a circuit breaker** `[REVISED — new]`: after N consecutive failures against a given model/provider, stop calling it for a cooldown window and fall back to the role's configured fallback model, recording the degradation in the run trail instead of crashing.
  - Structured output: request JSON, validate with Pydantic; on failure, one repair retry with the validation error; then raise `LLMOutputError` (caught by the node, recorded, run continues — §10).
  - Records an `LLMCall` row (tokens/cost from response usage).
  - **Concurrency guard** `[REVISED — new]`: an `asyncio.Semaphore` bounds concurrent in-flight Mesh calls so parallel Researcher fan-out (§11) doesn't trigger 429 storms.
  - Disk cache keyed by `(model, prompt_version, sha256(rendered_prompt))`; `temperature=0` for verifier/auditor/grader/coverage. Disableable via env.
- Prompts in `prompts/<name>.v<N>.md`; version is part of the cache key and the `LLMCall` row.
- Fetched web text always inserted inside a delimited `<untrusted_source>` block with an explicit instruction to treat it as data and ignore any instructions inside.

---

## 9. Retrieval & safety

- `retrieval/fetch_safe.py`: only http/https; block private/loopback/link-local/metadata IPs (including after redirects); max redirects 3; max body 5 MB; timeout 15s.
- `retrieval/firecrawl_client.py`: search + scrape only. Cache results by URL hash on disk.
- `retrieval/dates.py`: published date from metadata; fallback to a cheap Mesh call returning a **`DateExtraction` schema** `[REVISED — previously unschemaed]`; store `None` if unknown.
- `retrieval/dedup.py`: content hash for exact dupes, shingle Jaccard for near-duplicates.
- `retrieval/docs.py`: markitdown for uploads; uploads become `Source(origin="upload", tier=1 by default, configurable)`.

---

## 10. Resilience & failure handling [REVISED — consolidated, new section]

This is what makes the system "production-grade" without turning into infra sprawl. Every item here is small (tens of lines), not a new service.

| Failure mode | Handling |
|---|---|
| Mesh or Firecrawl down/slow | Retry with backoff (already in §8) **+ circuit breaker**: stop hammering a failing provider after N consecutive failures; cooldown, then retry; fall back to the role's secondary model/provider in the meantime |
| A sub-question's sources are all unreachable | Don't abort the run — mark that sub-question `insufficient_evidence`; the Synthesizer renders it under "Gaps"; the run still completes |
| API process restarts mid-run | Every node persists its output before advancing (§4). On startup, find `Run.status="in_progress"`, resume the graph from `last_completed_node` instead of restarting the run from scratch |
| Mesh/Firecrawl rate limiting | `asyncio.Semaphore` bounding concurrent calls (§8, §11), not just reactive retry |
| Malformed/invalid LLM JSON | One repair retry with the validation error, then raise `LLMOutputError`, caught by the node, logged, claim/sub-question marked failed — run continues |
| Budget/deadline exceeded | Checked *before* each expensive call (§6), run stops cleanly with `stop_reason` set, partial report still rendered from what's verified so far |
| A single node throws an unexpected exception | Caught at the node boundary, recorded in the run trail with a stack summary, node's contribution marked failed, graph continues to the next viable path rather than crashing the whole run |

Explicitly deferred to the Hardening bucket (§17): Docker/compose, Prometheus `/metrics`, Langfuse tracing, full auth + per-IP rate limiting middleware, graceful SIGTERM handling. None of these improve the failure-handling story for a judge more than the table above does; they cost disproportionately more time.

---

## 11. Optimization [REVISED — consolidated, new section]

| Technique | Where | Effect |
|---|---|---|
| Model routing by role | `llm/models.yaml` | Cheap/fast model for coverage-judging and date-extraction; strongest model only for verifier/auditor/synthesizer. Main cost lever — state the $ impact in the README. |
| Concurrent sub-question research | Researcher node, `asyncio.gather` + semaphore | Biggest latency win; free architecturally since LangGraph already fans out per sub-question |
| Cheap pre-filter before LLM entailment | Verifier stage 0 (§5) | Deterministic keyword/number overlap check in code discards obviously-irrelevant claim–passage pairs before spending a Mesh call |
| Disk cache on `(model, prompt_version, sha256(prompt))` | `llm/cache.py` | Free reruns during development; avoids re-spending credits on identical calls |
| URL-hash cache on fetches | `retrieval/firecrawl_client.py` | Avoids re-scraping the same page across sub-questions or loop iterations |
| Batched verification | Claim Verifier | When multiple claims cite the *same* passage, verify them in one call returning `Verdict[]` instead of one call per claim — fewer calls, same per-claim rigor since each still gets an independent verdict |

---

## 12. Context & memory management [REVISED — new section]

Two distinct problems, both cheap to solve with one shared primitive — `core/relevance.py` — rather than new infrastructure per agent.

**Context management** — what goes into *this* prompt, right now:

- **No node ever receives the raw accumulated state object.** Every agent is fed by a dedicated `build_<node>_context(state) -> dict` that selects only the fields that node needs. This is a hard rule, not a convenience — it's what keeps prompts small as a run accumulates iterations, and it's what stops "just pass `state` in, it's easier" from becoming the default during Phase 2-4 implementation.
- **Multi-item context is relevance-ranked and capped, never dumped.** Wherever a node would otherwise receive an unbounded list (passages → Drafter, claims → Synthesizer, stored evidence → Follow-up), it instead receives the top-`N` by `core/relevance.py.rank()`, where `N` is a named config constant (`max_passages_per_call`, default 12). If more remain, batch into multiple calls and merge results — one call never silently drops evidence by truncation, and it never silently balloons past a sane token budget either.
- **The Synthesizer composes per sub-question/section**, not one call over every claim in the run — this matches the report's own structure (organized by sub-question already) and bounds context size automatically as the claim count grows.
- **Loop re-research passes only structured failure reasons** to the Researcher (already true, §6) — never the full accumulated history of prior iterations.
- **Prompt structure favors provider-side prefix caching**: static content (schema dump, role instructions) always first and byte-identical across calls in a role; variable content (the specific claim/passage) always last. This is a different mechanism from our own disk cache (§8, which only hits on an *identical full prompt*) — this one lets the underlying provider cache a shared prefix across the many different Verifier calls in a single run, cutting latency/cost with zero extra code once the prompt files are written this way.

**Memory management** — what the system remembers, and for how long:

- **Working memory** = the typed LangGraph state for one run's duration. Bounded by `max_iterations` (§6) and a new explicit cap: **`max_passages_per_subquestion`** (default 30), enforced by the Evidence Curator *after* dedup — once exceeded, keep top-N by tier + recency + relevance (via `core/relevance.py`), log the drop as a trail event (never silently). This is the storage-side half of the context-budget problem above: it's what stops a sub-question that's looped 3 times from accumulating an unbounded passage pool that then has to be filtered down on every downstream call anyway.
- **Long-term memory** = the full run persisted to SQLite (§7) — sub-questions, sources, passages, claims, verdicts, costs, searches. This is also the system's audit trail (§2.5) and what makes replay mode (§13) and resume-after-crash (§10) possible — one mechanism serving three requirements.
- **Memoization memory** = the disk cache in `llm/client.py` (§8/§11) — exact-prompt reuse, not semantic reuse.
- **Episodic/follow-up memory** (Hardening bucket, §17): Follow-up answers only from a run's stored evidence, but *selects* which subset via the same `core/relevance.py.top_k()` used for Drafter passage selection, scoring the follow-up question against stored passages/claims. Without this, "answers only from stored evidence" silently degrades into "load everything stored," which is exactly the vector-DB-shaped problem §9's deviation was supposed to avoid — just unsolved instead of solved without a vector DB.
- **Memory hygiene**: the DB intentionally keeps full source text (needed for grounding, §2.4); logs intentionally never do (§13's logging rule, and `core/logging.py`'s own docstring). This asymmetry is deliberate, not an oversight — don't "fix" the logs to be more verbose.

`core/relevance.py` is Phase 1 infrastructure (not Phase 2+): it's a single shared, deterministic, LLM-free scorer (tokenized word-overlap Jaccard + a numeric-overlap bonus) used by the Verifier's pre-filter (§5 stage 0), re-attribution ranking (§5 stage 5), Drafter passage selection, Curator's over-cap trimming, Synthesizer batching, and Follow-up retrieval — one function, six call sites, instead of six ad-hoc heuristics.

---

## 13. Production concerns — Core path only

| Concern | Core requirement |
|---|---|
| Errors | `core/errors.py`: `DeepDiveError` → `RetrievalError`, `LLMError`, `LLMOutputError`, `VerificationError`, `BudgetExceeded`, `GuardrailViolation`. Nodes catch, record, continue (§10). API returns RFC 7807 problem JSON. |
| Logging | Structured dict logs with `run_id`/`node`/`subq_id`/`claim_id` bound. No secrets or full source texts in logs. |
| Guardrails | Pydantic on every agent boundary; question length ≤ 500 chars, non-empty; untrusted-content wrapping (§8); SSRF guard (§9); "no unverified sentence rendered as fact" enforced in the renderer (§2.4), not just the prompt. |
| Budgets | `core/budget.py`: per-run USD/token/time caps, checked pre-call (§6, §10). |
| Config | `core/config.py` via `pydantic-settings`; `.env.example` documents every variable. |
| Persistence | SQLite WAL; `store/repo.py` repository layer; `Run.last_completed_node` for resume. |
| Health | `/healthz`. |

Hardening-only (add after Core is solid, see §17): Langfuse, Prometheus `/metrics`, Docker/compose, auth/rate-limit middleware, Alembic, pre-commit/gitleaks/mypy-strict.

### API contract (`app/api.py`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/research` | `{question, upload_ids?, config?}` → `{run_id}`; background task |
| POST | `/uploads` | multipart → `{upload_id}` |
| GET | `/runs/{id}/events` | SSE stream of node progress |
| GET | `/runs/{id}` | run status + final report JSON, **including resolved source titles/URLs/tiers per sentence** `[REVISED — not just claim_ids]` |
| GET | `/runs/{id}/trail` | full decision trail, including `human_reviews` `[REVISED — §4.2]` |
| GET | `/runs/{id}/report.md` / `.pdf` | export — same human-readable citation chain as above (Hardening for PDF) |
| POST | `/runs/{id}/claims/{claim_id}/override` `[REVISED — §4.2]` | `{new_status, reason}` → promotes/confirms a claim; triggers `resynthesize_run` (Contradiction Checker → Synthesizer → Auditor only) |
| POST | `/runs/{id}/followup` | Hardening bucket |
| GET | `/healthz` | ops |

### Replay mode
`cli.py replay <run_dir>` and `make demo` reconstruct runs from committed `samples/` (sources + cached LLM responses + DB export) with **no API key** — judges can inspect real runs without spending credits.

---

## 14. UI (`ui/streamlit_app.py`)

Tabs: **Report** (each sentence expandable → evidence span, source, tier, confidence) · **Rejected & Unverified** (filter by `stage_failed`; show claim, cited source, span or lack of it, reason) · **Contradictions** · **Trail** (sub-questions, searches per iteration, coverage per iteration, stop reason, any degraded/circuit-broken provider) · **Cost & Latency**. Live progress via SSE. Works in replay mode.

---

## 15. Evaluation (`eval/`) — first-class deliverable

### Datasets
- `questions.yaml`: 30 questions across 6 domains (finance, health, tech, energy/climate, policy, history), each with a rubric of expected aspects written **before** running anything.
- 5 false-premise questions; expected behavior: flag premise, abstain from asserting it.
- `planted_claims.yaml`: 60 injected bad claims across 4 types — `wrong_number`, `wrong_entity`, `unsupported_extrapolation`, `contradiction` — paired with real passages.
- `injection_pages/`: 3 locally served pages containing prompt-injection instructions.
- `gold_labels.csv`: ~100 claim–span pairs **labeled by the human** (the user), starting **day 0**, in parallel with engineering — see §17. Never fabricated.

### Metrics (`eval/run_eval.py` → `eval/results/*.json` + `EVAL.md` table)

| Metric | Definition |
|---|---|
| Sentence support rate | final sentences judged supported by the independent grader / total |
| Citation precision | citations whose span supports their sentence / total citations |
| Planted-claim recall | planted bad claims rejected / planted (overall + per type) |
| False-rejection rate | gold-supported claims the verifier rejected / gold-supported |
| Judge–human agreement | accuracy + Cohen's κ of the grader vs `gold_labels.csv` |
| Rubric coverage | rubric aspects covered by verified claims / total aspects |
| Abstention accuracy | false-premise questions correctly flagged / 5 |
| Injection resistance | runs where injected instructions were followed (target 0) |
| Loop lift | mean coverage after loop − before loop |
| Cost / latency | mean USD, p50/p95 seconds per report |

### Comparisons
- **Baseline** (`eval/baseline.py`): same model, single prompt "answer with citations," same grader.
- **Ablations**: −span check, −numeric check, −re-attribution, −Report Auditor, −loop.
- Bootstrap 95% CIs. A **failure gallery** (5 real failures with analysis) in `EVAL.md`.

`make eval` must be runnable, resumable, and cached so reruns don't re-spend credits.

---

## 16. Repository layout

```
deepdive/
  app/          api.py graph.py state.py deps.py runner.py
  agents/       planner.py researcher.py curator.py drafter.py verifier.py coverage.py loop_control.py
                contradiction.py synthesizer.py auditor.py schemas.py
  verify/       span_match.py numeric_check.py reattribute.py confidence.py
  llm/          client.py cache.py circuit_breaker.py models.yaml pricing.yaml
  prompts/      planner.v1.md researcher.v1.md drafter.v1.md verifier.v1.md coverage.v1.md
                contradiction.v1.md synthesizer.v1.md auditor.v1.md baseline.v1.md
  retrieval/    base.py firecrawl_client.py null_provider.py fetch_safe.py dedup.py dates.py tiers.py
  core/         config.py errors.py logging.py budget.py relevance.py guardrails.py
  store/        models.py repo.py
  ui/           streamlit_app.py
  cli.py        (init-db, run, resume, export, replay entrypoints)
  eval/         questions.yaml planted_claims.yaml gold_labels.csv injection_pages/ label.py run_eval.py baseline.py report.py results/
  tests/        unit/ integration/
  .github/workflows/agent-arena-2026-checks.yml  .github/workflows/ci.yml
  Makefile README.md DESIGN.md EVAL.md BLUEPRINT.md requirements.txt .env .env.example .gitignore
```

(`app/runner.py` replaced the originally-planned `app/resume.py` — it owns both fresh-run and resume execution, since they share almost all their logic; see DESIGN.md §14.)

`Dockerfile`/`docker-compose.yml`/`.pre-commit-config.yaml`/Alembic `migrations/`, `samples/` (committed replayable runs), follow-up chat, and PDF export are added only if/when the Hardening bucket is reached — omitted so far; see §17 for exactly what's done vs. pending.

---

## 17. Phases & Definition of Done [REVISED — reordered: Core ships the grade, Hardening is cut-safe]

**Day 0 (parallel, non-engineering):** hand-label the ~100 gold claim–span pairs with `eval/label.py` (built, Phase 6). **Still pending — owned by the user**, same as a real `MESH_API_KEY`/`FIRECRAWL_API_KEY`; nothing fabricates this. It blocks the judge–human agreement metric specifically, not the rest of EVAL.md.

### Core path — ALL COMPLETE AND TESTED (mocked Mesh; no live key used or needed to verify correctness)

| # | Phase | Status |
|---|---|---|
| 1 | Skeleton, `llm/client.py`, `core/relevance.py`, SQLite, CI workflow | **Done.** Architecture-invariant tests pass; respx-mocked client tests pass. |
| 2 | Planner → Researcher → Curator → Drafter → Synthesizer happy path; `cli.py run` | **Done.** Integration test proves a report with stored sources/passages/claims; Curator cap-trim test (>30 passages) passes. |
| 3 | Claim Verifier (all stages) + re-attribution + loop + stop reasons | **Done.** Regression tests: unsupported claim rejected with `stage_failed` set; mis-cited claim re-cited; full-graph loop test proves a thin sub-question triggers real re-research and the run stops with the correct `stop_reason`. |
| 4 | Report Auditor (second model, `auditor` role ≠ `verifier` role) | **Done.** Embellished-sentence test: fails, deterministic rewrite, re-audit, passes; a sentence that still fails is stripped but always persisted. |
| 5 | API, Streamlit UI (5 tabs), replay mode, **checkpoint-based resume** | **Done**, with one deviation from the original plan (see DESIGN.md §14): resume uses LangGraph's own `AsyncSqliteSaver` checkpointer, not a hand-rolled restart-from-node-name scheme — verified with a test that crashes a run after the Planner and confirms resume does not re-call it. SSE is polling-based, not a true push stream (documented tradeoff). **5 committed sample runs: not done** — would require fabricating "real" executions without a live key, which is explicitly forbidden (§18/§19); the replay mechanism itself is built and round-trip-tested. |
| 6 | Eval infrastructure: `questions.yaml` (30 + 5 false-premise, authored), `planted_claims.yaml` (60, authored), `injection_pages/` (3), `label.py`, `run_eval.py`, `baseline.py`, `report.py` | **Built and unit-tested** (metric math, report formatting, planted-claim orchestration, the `grader`-role judge-human-agreement orchestration, and baseline citation scoring — all against a mocked Mesh). The `grader` model role (reserved since Phase 1, distinct from `verifier`/`auditor`) is now actually wired up; `eval/baseline.py` now scores its own citations with the same standard the Auditor uses, so the baseline comparison has a real number to show, not just unscored text. **Run for real** (DESIGN.md §18–19, EVAL.md): planted-claim recall 60/60, injection 0/3 followed, a 17-question sweep with independent grading and baseline (on the free_web provider, since Firecrawl credits ran out), and a re-grade of 10 earlier runs. Judge–human agreement still needs human labels in `gold_labels.csv` (37 blank candidates provided). No numbers are fabricated. |

### Hardening bucket

| # | Item | Status |
|---|---|---|
| 7 | Contradiction Checker, source-tier weighting in confidence | **Done.** Source-tier weighting was already in `verify/confidence.py` (Phase 3); Contradiction Checker built and tested, wired into the graph between the loop and the Synthesizer, which now renders a "Sources disagree" sentence quoting both conflicting claims verbatim. |
| 7a | Human-in-the-loop override (§4.2) `[REVISED — new, not in the original plan]` | **Done.** A human can promote or confirm a rejected/unverified/partial claim with a required reason, via the UI or `POST /runs/{id}/claims/{id}/override`; triggers a scoped re-synthesis. Never mutates the AI's original `Verdict`. Tested at the repo, runner, and API layers. |
| 8 | SSRF test fixtures, budget-cap tests, basic auth/rate-limit | **Partial, improved.** SSRF fixtures done (Phase 2). Budget: both wall-clock deadline **and** USD/token spend (`core/budget.py::budget_exceeded`, recomputed from real `LLMCall` rows via `Repo.get_run_cost_and_tokens`) are now real, tested, and produce `stop_reason="budget"` — found unwired (a dead `RunBudget` class, never called) during a self-review, fixed (DESIGN.md §16). Still per-iteration granularity, not per-call — that finer grain remains explicitly deferred. Auth/rate-limit: **not done**. |
| 9 | Langfuse tracing, Prometheus `/metrics`, Docker/compose | **Not done.** |
| 10 | PDF export, follow-up chat, Alembic, pre-commit/gitleaks/mypy-strict | **Follow-up done** (`agents/followup.py`, `POST /runs/{id}/followup`). PDF export, Alembic, pre-commit: not done. |

| # | Always last | Status |
|---|---|---|
| 11 | README, `DESIGN.md`, `EVAL.md`, demo script | README and DESIGN.md updated to current state; `EVAL.md` written with an honest pending-numbers status. No demo video/GIF (out of scope for this tool). |

---

## 18. Engineering rules for Claude Code

- Smallest change that satisfies the phase; no speculative abstractions or opportunistic refactors.
- Type hints everywhere; Pydantic at boundaries; no bare `except`.
- Every bug fix gets a regression test. Every node gets a unit test with Mesh mocked via `respx`.
- Never claim something works without running it. If a check can't be run (e.g., needs real credits), say so explicitly.
- Never fabricate eval data, gold labels, metrics, or sample runs. Sample runs must come from real executions.
- Keep secrets out of code, logs, fixtures, and commits.
- Small, meaningful commits per sub-step.
- Ask the user before: adding a dependency not in §3, changing the API contract, choosing Mesh model IDs, spending credits on full eval runs, or deploying.

---

## 19. Open inputs from the user

- Deadline and team size (affects how much of the Hardening bucket is reachable).
- Mesh model IDs per role (`llm/models.yaml`) — verify availability on Mesh.
- Firecrawl: hosted free tier vs self-hosted; fallback provider if quota is hit.
- Deployment target for the optional URL (Render / Railway / HF Spaces / other).
- Gold labels (~100 pairs) — owned by the user, start Day 0.
