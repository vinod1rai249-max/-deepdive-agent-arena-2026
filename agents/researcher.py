"""Researcher agent (BLUEPRINT.md §4.1, §11): runs concurrently across
sub-questions (asyncio.gather + semaphore), generates search queries via
Mesh, then searches and scrapes via the injected SearchProvider. A
sub-question whose search/scrape fails entirely doesn't crash the run —
it just ends up with no sources, flagged downstream as
`insufficient_evidence` rather than silently padding the report
(BLUEPRINT.md §10)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from urllib.parse import urlparse

from agents.schemas import QueryPlan
from app.deps import Deps
from app.state import SourceState, SubQuestionState
from core.errors import GuardrailViolation, RetrievalError
from core.logging import get_logger
from retrieval.dates import parse_published_date
from retrieval.fetch_safe import assert_safe_url
from retrieval.tiers import classify_tier

logger = get_logger(__name__)

MAX_RESULTS_PER_QUERY = 5


async def _generate_queries(deps: Deps, run_id: str, subq: SubQuestionState, failure_reason: str | None) -> list[str]:
    try:
        plan: QueryPlan = await deps.mesh.complete_json(
            node="researcher",
            prompt_name="researcher.v1",
            variables={
                "sub_question": subq["text"],
                "acceptance_criteria": "; ".join(subq["acceptance_criteria"]),
                "failure_reason": failure_reason or "none — this is the first research pass.",
            },
            schema=QueryPlan,
            model_role="cheap",
            run_id=run_id,
        )
        return plan.queries
    except Exception:
        logger.warning("query generation failed, falling back to raw sub-question text", exc_info=True)
        return [subq["text"]]


async def _research_one(
    deps: Deps, run_id: str, subq: SubQuestionState, failure_reason: str | None = None
) -> list[SourceState]:
    queries = await _generate_queries(deps, run_id, subq, failure_reason)

    sources: list[SourceState] = []
    seen_hashes: set[str] = set()

    for query in queries:
        try:
            results = await deps.search.search(query, limit=MAX_RESULTS_PER_QUERY)
        except RetrievalError:
            logger.warning("search failed for query", extra={"extra_fields": {"subq_id": subq["id"], "query": query}})
            continue

        deps.repo.record_search(run_id, subq["id"], query, provider=deps.search.name, result_count=len(results))

        for result in results:
            try:
                assert_safe_url(result.url)
            except GuardrailViolation:
                logger.warning("skipped unsafe URL", extra={"extra_fields": {"url": result.url}})
                continue

            try:
                page = await deps.search.scrape(result.url)
            except RetrievalError:
                continue
            if not page or not page.text:
                continue

            content_hash = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            domain = urlparse(page.url).netloc
            published_dt = parse_published_date(page.published_at)
            sources.append(
                SourceState(
                    id=str(uuid.uuid4()),
                    subq_id=subq["id"],
                    url=page.url,
                    domain=domain,
                    title=page.title,
                    tier=classify_tier(domain),
                    content_hash=content_hash,
                    text=page.text,
                    origin="web",
                    published_at=published_dt.isoformat() if published_dt else None,
                )
            )

    return sources


async def research_all(
    deps: Deps,
    run_id: str,
    sub_questions: list[SubQuestionState],
    failure_reasons: dict[str, str] | None = None,
) -> list[SourceState]:
    failure_reasons = failure_reasons or {}
    semaphore = asyncio.Semaphore(deps.settings.mesh_max_concurrency)

    async def bounded(subq: SubQuestionState) -> list[SourceState]:
        async with semaphore:
            return await _research_one(deps, run_id, subq, failure_reasons.get(subq["id"]))

    results = await asyncio.gather(*(bounded(sq) for sq in sub_questions), return_exceptions=True)

    sources: list[SourceState] = []
    for subq, result in zip(sub_questions, results):
        if isinstance(result, BaseException):
            logger.error(
                "sub-question research failed entirely; continuing without it",
                extra={"extra_fields": {"subq_id": subq["id"], "error": str(result)}},
            )
            continue
        sources.extend(result)

    if sources:
        deps.repo.create_sources(run_id, sources)
    return sources
