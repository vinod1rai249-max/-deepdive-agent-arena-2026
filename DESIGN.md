# DeepDive — Design Decisions

This document records why DeepDive is built the way `BLUEPRINT.md` describes: the decisions made, the alternatives rejected, and the tradeoffs accepted. It exists so a judge (or a future contributor) can see the reasoning, not just the result — and so the rationale survives even though it was arrived at before a single line of code was written.

---

## 1. Why this architecture, not a simpler one

The brief's own weakness statement — "tools that produce cited reports from one question already exist... the cited source often does not actually support the sentence attached to it" — is the thing being optimized for. A single-prompt RAG wrapper can produce citations; it cannot produce *verified* citations, because nothing checks the model's own claim about what a source says. DeepDive's core bet is: **the verifier's job is not to ask the model if it's right, it's to make the model show its work and then check the work with code.**

That's why the pipeline has two independent layers checking every claim:
- An LLM layer that extracts *what it thinks* the source says (`evidence_span`) and *whether* that supports the claim (`entailment`).
- A code layer (`rapidfuzz` span matching, numeric/entity extraction) that verifies the LLM's own extracted span actually exists in the stored text, and that specific numbers/entities weren't silently altered.

A model can lie about entailment; it's much harder for it to fabricate a verbatim span that also happens to fuzzy-match real text it never saw. This two-layer design is the single most load-bearing decision in the system.

## 2. Why LangGraph over a custom orchestrator or CrewAI/AutoGen

The brief's bonus explicitly names "a real loop back to research when verification fails" as a differentiator. LangGraph's conditional edges make that loop a first-class, inspectable part of the graph (`coverage < τ → back to Researcher`) rather than a manually-coded retry wrapper. CrewAI/AutoGen's role-based delegation model is a worse fit for a pipeline with a genuine conditional loop and per-node persistence requirements — we'd end up re-implementing graph semantics on top of them anyway.

## 3. Why citations are a foreign-key chain, not LLM-authored text

Early design risk: even with a verified claim, the *URL stapled to it in the final report* could still be something the Synthesizer typed from memory rather than the actual source. **Rule: the renderer never accepts a URL from an LLM.** It resolves `Source.url` by following `Claim.passage_id → Passage.source_id → Source` — a database lookup, not a generation. This is why the API and export formats carry resolved source titles/URLs/tiers per sentence, not just internal `claim_ids` — a judge reading the JSON or PDF should be able to verify "this citation supports this sentence" without touching the database.

## 4. Why the Verifier and Report Auditor must use different models

If the same model drafts, verifies, and audits its own writing, errors introduced in drafting are more likely to pass a verification step written in the model's own "style" of reasoning. Using a second model for the Auditor is cheap insurance against correlated blind spots, and it's the kind of detail that's easy to point to in an interview as evidence of actually thinking about grading bias, not just cost.

## 5. Why re-attribution exists (rather than just reject-on-fail)

A claim can be *true and well-supported* but mis-cited — the Drafter picked the wrong passage among several retrieved for the same sub-question. Rejecting outright would conflate "false claim" with "correct claim, wrong footnote," understating groundedness and overstating the system's actual error rate. Re-attribution (try the top-k other passages before giving up) keeps the false-rejection rate honest, which matters directly for one of our own eval metrics.

## 6. Why a cheap deterministic pre-filter sits in front of the LLM verifier stage

Most claim–passage pairs considered for re-attribution or batch verification are obviously irrelevant (no keyword/number overlap at all). Spending an LLM call to establish that is pure waste. The pre-filter is free, deterministic, and only ever *adds* a cheap gate before the expensive step — it cannot itself mark something verified, so it can't weaken grounding.

## 7. Why resumability (not just retries) was added to the Core path, not Hardening

A FastAPI background task dies if the process restarts. Everything else in the resilience table — circuit breakers, graceful per-sub-question degradation, budget pre-checks — protects a run that's still alive. Resume protects against the one failure mode that silently destroys a run that was otherwise working: a crash or redeploy mid-execution. Because every node already persists its output before advancing (required for the decision trail anyway), resume costs almost nothing extra to add — it's just "find `status=in_progress` runs and re-enter after `last_completed_node`" — so it stayed in the Core path instead of Hardening.

## 8. Why Docker, Prometheus, Langfuse, and Alembic are deliberately deferred

None of these improve what the brief actually judges: distinct orchestrated agents, a verifier that rejects claims, trustworthy citations, a visible trail, honest numbers. They're real engineering practice, but in a time-boxed build they compete directly with Phases 3/4/6 (verifier, auditor, eval) — which the blueprint marks non-cuttable for a reason. Alembic specifically buys nothing without live production data to migrate across schema versions; `create_all` is sufficient for a project with one schema, built once, by one team, before any users exist. If time remains after the Core path and the first Hardening items (contradiction checker, source tiers, SSRF tests), these get added — but shipping without them costs nothing on the judged criteria.

## 9. Explicit deviation: no vector DB for uploaded documents

The suggested stack mentions a vector DB "for grounding over documents." DeepDive intentionally does not add one: uploaded documents are chunked into `Passage`s using the same character-offset chunking as web content, rather than embedded and semantically retrieved. Rationale: this keeps Mesh as the system's only AI dependency (embeddings would otherwise be a second AI surface to route and justify) and avoids introducing Chroma/Qdrant/Pinecone for what is, in a hackathon-scale demo, a small number of uploaded files per run. The tradeoff: a very large uploaded document could exceed the Drafter's effective context more gracefully with semantic retrieval than with naive chunking. Accepted for v1; worth revisiting if uploads become a primary use case rather than an optional one.

## 10. Why model routing by role is the primary cost/latency lever

Verification and auditing are where correctness has to be spent; coverage-judging, date extraction, and query generation are comparatively low-stakes classification-shaped tasks where a cheap/fast model is indistinguishable in quality but materially cheaper and faster. Routing by role (`llm/models.yaml`) turns "optimize cost" from a vague goal into a one-file decision that's visible and explainable — worth stating the actual $ breakdown in the README once real numbers exist.

## 11. Why the phase order was reversed from the original draft

The original draft sequenced Alembic, structured logging, disk caching, and full CI tooling into Phase 1 — before a single agent produced a single report. That ordering optimizes for "this looks like a mature codebase" over "this demonstrably does the hard thing the brief is judging." The revised order (§16 of `BLUEPRINT.md`) gets a real cited report out of the system by the end of Phase 2, a working verifier that rejects a real claim by the end of Phase 3, and real self-eval numbers by the end of Phase 6 — with all production hardening explicitly optional after that. If the project runs out of time, it runs out of time *after* the parts that are actually scored.

## 12. Why gold-label collection starts Day 0, outside the phase sequence

Hand-labeling ~100 claim–span pairs is the one task in the entire project that only the user can do — Claude Code cannot generate ground truth without it being fabrication, which is explicitly forbidden (`BLUEPRINT.md` §17). Scheduling it in Phase 6, as the original draft did, risks reaching the eval phase with no labels and therefore no judge–human agreement number — one of the more credible metrics in the self-eval table. Starting it in parallel with Phase 1 engineering removes it from the critical path entirely.

---

## 13. Why context and memory management share one scorer instead of per-agent heuristics

Two different problems hide under "memory": what goes into any single prompt right now (context management), and what the system remembers across calls and runs (memory management). Both were under-specified in the first draft — nothing capped how many passages the Drafter could receive, nothing bounded how large a sub-question's evidence pool could grow across loop iterations, and the Follow-up feature's "answers only from stored evidence" had no stated selection mechanism, which would have silently turned into "load everything stored" as soon as a run accumulated enough evidence.

The fix (`BLUEPRINT.md` §12) is one shared primitive, `core/relevance.py` — a deterministic, LLM-free lexical scorer (tokenized word-overlap Jaccard plus a numeric-overlap bonus) — reused at every point that needs to rank or filter text against a query: the Verifier's pre-filter, re-attribution's passage ranking, the Drafter's passage selection, the Curator's over-cap trimming, and Follow-up's evidence retrieval. One function, six call sites, rather than six slightly-different ad-hoc heuristics that would drift apart over time and be individually harder to trust.

This also keeps the "no vector DB" decision (§9) honest. It would have been easy to solve Follow-up's retrieval problem by reaching for embeddings "just for that one feature," which quietly reintroduces the exact dependency §9 argued against. Reusing the lexical scorer instead means the no-vector-DB decision applies uniformly, not just to the parts of the system that were built first.

The caps themselves (`max_passages_per_call=12`, `max_passages_per_subquestion=30`) are deliberately generous defaults, not tuned constants — they exist so that a pathological case (a sub-question that's looped three times and accumulated 80 passages) degrades to "the system picks the 30 most relevant and logs that it dropped the rest" instead of either silently truncating evidence with no record of it, or stuffing all 80 into one Drafter call and quietly degrading claim-extraction quality by diluting the model's attention across irrelevant text.

## 14. Why resume uses LangGraph's checkpointer instead of the planned `last_completed_node` restart

The original plan (and `BLUEPRINT.md` §10 as first written) was to resume a crashed run by recording `last_completed_node` on the `Run` row and re-entering the graph after that node on restart. That's workable, but it only remembers a node *name* — reconstructing the actual state to resume with (which sources, which passages, which claims had already been drafted) would have meant re-deriving it from the DB by hand for every node type, or re-running nodes that had already succeeded "just to rebuild state," which defeats the point.

LangGraph ships its own answer to exactly this problem: a checkpointer (`langgraph-checkpoint-sqlite`'s `AsyncSqliteSaver`) that snapshots the full typed state after every node, keyed by a `thread_id`. Using the run's own `id` as the `thread_id` means `graph.ainvoke(None, config={"configurable": {"thread_id": run_id}})` resumes from exactly the last successful node with its full state intact — no hand-written state reconstruction, no re-running completed work. This was verified directly before being relied on: a standalone two-node graph where the second node fails once and succeeds on retry confirms the first node is never called twice.

`last_completed_node` is kept on `Run`, but its role changed: it's now a human-readable trail field ("how far did this get") for the UI and logs, not the resume mechanism. The actual resume test (`tests/unit/test_runner.py`) simulates a crash via an unhandled DB-write error — deliberately not a Mesh-call failure, since every agent-level Mesh call in this codebase degrades gracefully by design (§10) and genuinely cannot crash a run; proving resume meant finding a failure mode that *isn't* caught, which is itself a useful confirmation that the graceful-degradation design is as thorough as intended.

One consequence worth stating plainly: resumability now depends on `langgraph-checkpoint-sqlite`, a dependency not in the original `BLUEPRINT.md` §3 stack list. It's small, it's SQLite (consistent with the rest of the stack), and the alternative was a materially weaker, hand-rolled mechanism — accepted as a justified addition rather than scope creep.

## 15. Why a human-in-the-loop override was added, and why only this one

This track's brief asks for a *self-verifying* system — the audit trail requirement ("a human can see exactly what was kept and what was rejected") is satisfied by letting a human read the trail after a run completes, not by requiring their approval during one. So strictly, no part of this system needed a human in the loop to meet the brief.

Several candidate HITL points were considered anyway, since a review pass surfaced them as plausible value-adds: a pre-publish approval gate, a pause-for-confirmation when the Planner flags a false premise, a contradiction-resolution step, and a feedback loop from judge/human disagreement back into prompt tuning. All but one were rejected:

- A pre-publish gate and a contradiction-resolution step add process without adding information the pipeline doesn't already surface (the trail and the "Sources disagree" sentence already make the ambiguity visible).
- A premise-flag confirmation pause is actively counterproductive: it would make the Planner's abstention behavior depend on an un-automatable human step, which conflicts directly with the "abstention accuracy" metric `EVAL.md` is supposed to measure automatically. Adding it would make the exact thing being evaluated un-evaluable without a human present for every run.
- A judge/human-disagreement feedback loop is a real idea, but it's active-learning scope — ongoing prompt/threshold calibration — not a feature with a clean boundary to ship now.

**The one kept: a human can promote or confirm a claim the Verifier rejected, unverified, or only partially supported.** This is the one place a human adds judgment the pipeline structurally cannot have — their own read of a borderline case, e.g. a span-match near-miss that was actually fine, or a numeric check that was too literal. Two design choices make this safe rather than a backdoor around the verification story:

1. **The override never mutates the original `Verdict`.** It's a separate `HumanReview` row. A reader (or a judge) can see *both* "the AI said X because Y" and "a human overrode it to Z because W" — nothing is silently replaced, which is the same principle the whole trail is built on (§2.5).
2. **It triggers a scoped re-synthesis, not a magic patch.** `resynthesize_run` re-runs Contradiction Checker → Synthesizer → Report Auditor for real, through the same Mesh calls and the same Auditor check every other sentence goes through. A promoted claim doesn't get a free pass into the report — it still has to survive the Auditor's independent check against its own evidence, same as if the Verifier had passed it originally. The only thing that changed is which claims were eligible to be cited.

Every run still completes fully autonomously with zero human involvement — this is additive, not a dependency.

## 16. What the first live run against real Mesh + Firecrawl actually broke

Everything up to this point had passed fully mocked tests. Pointing the system at a real `MESH_API_KEY` and `FIRECRAWL_API_KEY` for the first time surfaced four real issues mocks couldn't — the reason this section exists at all, and the reason ~12 code comments elsewhere in this codebase cite it:

1. **A placeholder verifier model ID didn't exist on Mesh.** `claude-3-5-sonnet-20241022` 404'd on every call. Fixed by querying Mesh's real `/v1/models` catalog live and swapping in confirmed-working IDs (`llm/models.yaml`), with `llm/pricing.yaml` corrected to match.
2. **Claude (via Mesh) wraps JSON in markdown fences despite `response_format` and explicit instructions not to.** `llm/client.py` now strips fences before every parse attempt (cache-hit, main parse, repair-retry), and caches the *cleaned* text, not the raw fenced text.
3. **The more serious bug: a schema failure couldn't fall back to another model.** `except LLMOutputError: raise` meant the fence bug above was fatal instead of recoverable — removing that special case so `LLMOutputError` falls through like any other exception is what actually made the fence fix effective, not the fence fix alone.
4. **`SubQuestion.coverage` was computed and used correctly in-memory but never written back to the DB.** Every run showed 0% coverage in the trail regardless of real progress. Fixed with one missing `UPDATE` (`Repo.update_subquestion_coverage`, called from `loop_control.decide()`).

A fifth change from the same testing round, not a bug fix but a real latency improvement found while manually exercising the UI: the Verifier, Drafter, Contradiction Checker, and loop-control's coverage pass were each looping over sub-questions **sequentially** when nothing about them was actually dependent — switched to `asyncio.gather`, relying on `MeshClient`'s own internal semaphore for real throttling rather than an ad-hoc one per call site.

An unplanned bonus: while bug 3 was live and unfixed, real concurrent traffic from outside this session hit it, degraded gracefully instead of crashing (every agent-level Mesh failure is absorbed by design), and was left `in_progress` when the server was killed to deploy the fix. On restart, the checkpointer-based resume (§14) picked both runs up and completed them cleanly — an accidental, real-world validation of graceful degradation *and* resume-after-crash at the same time.

## 17. Four gaps found by auditing the spec against the code, not just reading the spec

A later self-review asked a sharper question than usual: not "does the blueprint describe this correctly," but "does the *code* actually do what the blueprint says it does." Grepping for the things §8 and §6 promised turned up four real gaps — not fabrications in the docs, just principles that were written down and never wired in:

1. **The `<untrusted_source>` wrapping §8 described was never implemented.** `eval/injection_pages/` existed specifically to test a defense that didn't exist yet in any prompt. Fixed: `core/guardrails.py::wrap_untrusted()`, applied at every point raw scraped text reaches a prompt — the Drafter's passages, the Verifier's passage, the Auditor's evidence, the eval baseline's fetched evidence. Deliberately *not* applied to claim text, sentence text, or the question itself — those are model-generated or user-provided, not the injection vector; wrapping them would just add noise.
2. **`core/budget.py` had a `RunBudget` class nothing ever called.** The deadline check in `agents/loop_control.py` was real; the USD/token half of "budget" was dead code with a docstring that claimed otherwise. Fixed by replacing the stateful, unused class with a plain function (`budget_exceeded`) that matches how it's actually invoked — recomputed from real `LLMCall` rows each iteration, not accumulated in memory — and wiring it into the same stop-reason chain as deadline/max_iter/no_new_evidence.
3. **`core/logging.py`'s `bind()` context mechanism was built and never called.** Every graph node now binds `run_id`/`node` at its own entry point (`app/graph.py`) — redundantly from the run-level bind in `app/runner.py`, deliberately, so each node's logs are correct even if LangGraph schedules nodes as separate asyncio tasks with independently-copied contexts (Python's `contextvars` semantics, not something this codebase controls).
4. (Not a gap, documented for completeness) — the diagram, bug-table framing, and architecture-table additions discussed in this round were a README presentation exercise, not a code change; no implementation decision to record.

The pattern worth naming: **"the spec says X" and "the code does X" are different claims**, and only grepping the code answers the second one. All three real fixes above have regression tests specifically because of that distinction — a docstring asserting something is wired in is not evidence that it is.

## Open inputs still owned by the user

These are decisions `BLUEPRINT.md` §18 flags as blocking and that this document cannot resolve on its own:

- Deadline and team size — determines how much of the Hardening bucket is realistically reachable.
- Mesh model IDs per role — needs checking against what's actually available on Mesh before `models.yaml` is finalized.
- Firecrawl plan (hosted free tier vs self-hosted) and a fallback search provider if quota is hit mid-build or mid-judging.
- Deployment target for the optional live URL, if pursued.
- The gold-label set itself (Day 0 task above).

## Known risk accepted, not solved

Even with the two-layer verification design, a sufficiently well-written adversarial source (e.g., a page crafted to pass span-matching while being subtly wrong) could still slip through. The injection-resistance and planted-claim-recall metrics in `eval/` are the system's own early-warning mechanism for this, not a guarantee against it — this is stated honestly rather than claimed as solved, consistent with the project's own grounding principle: don't assert what isn't verified.

## 18. Audit round 2: five gaps found by reviewing the submission against the brief

An external review of the repo against the hackathon brief found five things the docs claimed or implied that the code did not do. Each was fixed with a test, and the self-evaluation was actually run.

1. **"Verifier and Auditor are always different models" was false under fallback.** `verifier.fallback` was `openai/gpt-4o`, which is the auditor's primary. In the first showcase EV run, 269 of 297 verifier calls ran on gpt-4o, the same model that audited the report. Fix: verifier, auditor and grader now share **no** model, primary or fallback (`llm/models.yaml`: haiku-4.5 → gemini-2.5-flash; gpt-4o → gemini-2.5-pro; sonnet-5 → sonnet-4.6), enforced by `tests/unit/test_model_roles.py`. The EV question was re-run on the corrected config as part of the eval (`fin-01`).
2. **Self-evaluation was built but never run.** `eval/run_eval.py` now also measures the independent-grader support rate (a third model re-judges every cited final sentence against its stored passage), rubric coverage, abstention, claim pass rate, cost and latency, writes results incrementally, and is resumable. The numbers are in `EVAL.md`, quoted from `eval/results/*.json`.
3. **Confidence scoring ignored two of its four documented inputs.** `corroborating_sources` was hard-coded to 1 and `time_scope` to `None`. Now `agents/verifier.py::_apply_corroboration` counts distinct *other* sources with a lexically near-identical passed claim (same `core/relevance.py` scorer, no LLM), and `verify/confidence.py` penalises sources more than a year older than the sub-question's `time_scope`.
4. **The report had no "Gaps / Unverified" section.** `agents/synthesizer.py::build_gaps_sentences` appends, deterministically and without any LLM, a note for each thin sub-question and for each rejected/unverified claim with the stage that rejected it. These notes carry no `claim_ids` on purpose: a rejected claim's source does not support it, so citing it would be exactly the failure this system exists to prevent.
5. **Each report sentence was stored twice** (once as a synthesizer draft, once after audit), so "failed audits" looked ~6× worse than reality (56 vs 9 on the EV run). The Auditor is now the only writer of `report_sentences`.

Also removed: Firecrawl's `check_prompt_injection` flag (a third-party-side classifier; the brief says no third-party tool may make AI calls on our behalf), and the unused `demo_mode` setting.

**Known limitation not fixed:** `Verdict.model` stores the *role* name (`"verifier"`), not the concrete model ID; the concrete model per call is in `llm_calls`. Judge–human agreement is still unmeasured because `eval/gold_labels.csv` needs human labels and we do not fabricate them.

## 19. Audit round 3: closing the evaluation, and what it found

With Firecrawl's credits exhausted until after the deadline, the second `SearchProvider` that BLUEPRINT §10 called for was built (`retrieval/free_web_provider.py`: Wikipedia, arXiv, PubMed, plus a safe plain fetch; `retrieval/fallback_provider.py` switches to it only on a credit-exhaustion error, never on a transient one). The full sweep then ran on it, and the results changed the code:

1. **The `partial` loophole was real and is fixed, with a caveat.** `verify/scope_check.py` requires a scope-widening word in a claim (overall, entire, national, global, every, most, never…) to appear in the cited passage. Planted recall went 57/60 → 60/60. The word list was written after seeing the three misses, so that number is a regression result, not a generalisation claim; applied to 625 real claims it would reject 11 (1.8%), at least one dubiously.
2. **The abstention metric was hollow.** The Planner flagged all 5 false premises, but nothing in the report told the reader, and two reports never addressed the premise. The flag is now rendered as a deterministic "Premise check" note (it says the premise was not assumed and not established — it does not claim to prove it false).
3. **Two judges disagree in an informative way.** The independent grader finds ~26% of cited sentences only partly supported, and several that our Auditor passed while its own reason described the gap. A country substitution (Costa Rica → "the US") passed both the entailment judge and the code checks. Unfixed; the proposed fix is in `EVAL.md` §5.
4. **Follow-up** (`agents/followup.py`) reuses a run's stored passages and re-verifies every answer sentence with the same span/numeric/scope checks; unsupported sentences are dropped and an off-topic question gets an explicit "not in the stored research".
5. **Injection resistance is now measured** (`eval/injection.py`): each hostile page is the only source of a real run, plus a planted false claim per page. 0/3 followed — a smoke test of our own defence, not an adversarial audit.

Not done: human labels (37 candidate pairs are in `eval/gold_labels.csv`, blank — only a person can fill them), ablations, PDF export, a Firecrawl-backed re-run.
