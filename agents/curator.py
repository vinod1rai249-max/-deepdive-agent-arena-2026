"""Evidence Curator (BLUEPRINT.md §4.1, §7, §12): deterministic code, no
LLM. Near-duplicate merge across sources for the same sub-question,
sentence-aligned passage chunking, and the `max_passages_per_subquestion`
cap — enforced after dedup, logged (never silent) when it trims, ranked by
relevance + source tier rather than arbitrary truncation."""

from __future__ import annotations

import uuid

from app.deps import Deps
from app.state import PassageState, SourceState, SubQuestionState
from core.logging import get_logger
from core.relevance import score
from retrieval.dedup import is_near_duplicate

logger = get_logger(__name__)

PASSAGE_CHARS = 800


def _chunk_sentence_aligned(text: str, size: int = PASSAGE_CHARS) -> list[tuple[int, int, str]]:
    """Split `text` into ~`size`-char chunks, breaking on sentence
    boundaries where possible rather than mid-word."""
    chunks: list[tuple[int, int, str]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            boundary = text.rfind(". ", start, end)
            if boundary != -1 and boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((start, end, chunk))
        start = end if end > start else start + size
    return chunks


def _dedup_sources(sources: list[SourceState]) -> list[SourceState]:
    survivors: list[SourceState] = []
    for source in sources:
        if any(is_near_duplicate(source["text"], kept["text"]) for kept in survivors):
            continue
        survivors.append(source)
    return survivors


def curate(
    deps: Deps,
    run_id: str,
    sub_questions: list[SubQuestionState],
    sources: list[SourceState],
    existing_passages: list[PassageState] | None = None,
) -> list[PassageState]:
    """`sources` should be only the NOT-yet-curated sources (the graph
    only ever passes new ones in — BLUEPRINT.md §12 "append-only, skip
    already-processed"). `existing_passages` is this run's full passage
    set so far, used only to figure out how much headroom is left under
    the cap — existing passages are never re-trimmed or regenerated here,
    since they may already have verified claims pointing at them."""
    existing_passages = existing_passages or []
    existing_count_by_subq: dict[str, int] = {}
    for p in existing_passages:
        existing_count_by_subq[p["subq_id"]] = existing_count_by_subq.get(p["subq_id"], 0) + 1

    subq_by_id = {sq["id"]: sq for sq in sub_questions}

    by_subq: dict[str, list[SourceState]] = {}
    for source in sources:
        by_subq.setdefault(source["subq_id"], []).append(source)

    all_new_passages: list[PassageState] = []
    cap = deps.settings.max_passages_per_subquestion

    for subq_id, subq_sources in by_subq.items():
        deduped = _dedup_sources(subq_sources)
        source_by_id = {s["id"]: s for s in deduped}

        passages: list[PassageState] = []
        for source in deduped:
            for start, end, chunk in _chunk_sentence_aligned(source["text"]):
                passages.append(
                    PassageState(
                        id=str(uuid.uuid4()),
                        source_id=source["id"],
                        subq_id=subq_id,
                        start=start,
                        end=end,
                        text=chunk,
                    )
                )

        allowed_new = max(0, cap - existing_count_by_subq.get(subq_id, 0))
        if len(passages) > allowed_new:
            subq = subq_by_id.get(subq_id)
            query = f"{subq['text']} {' '.join(subq['acceptance_criteria'])}" if subq else ""

            def combined_score(passage: PassageState) -> float:
                relevance = score(query, passage["text"])
                src = source_by_id.get(passage["source_id"], {})
                tier_bonus = (5 - src.get("tier", 4)) * 0.01
                recency_bonus = 0.005 if src.get("published_at") else 0.0
                return relevance + tier_bonus + recency_bonus

            passages.sort(key=combined_score, reverse=True)
            dropped = len(passages) - allowed_new
            logger.warning(
                "curator trimmed passages over max_passages_per_subquestion",
                extra={"extra_fields": {"subq_id": subq_id, "dropped": dropped, "cap": cap, "total": len(passages)}},
            )
            passages = passages[:allowed_new]

        all_new_passages.extend(passages)

    if all_new_passages:
        deps.repo.create_passages(all_new_passages)
    return all_new_passages
