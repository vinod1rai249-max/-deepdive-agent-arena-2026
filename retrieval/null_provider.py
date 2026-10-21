"""Fallback SearchProvider used when no Firecrawl key is configured
(BLUEPRINT.md §10 graceful degradation): a run still completes end to end
— every sub-question just ends up `insufficient_evidence` instead of the
whole pipeline crashing on a missing dependency."""

from __future__ import annotations

from retrieval.base import ScrapedPage, SearchProvider, SearchResult


class NullSearchProvider(SearchProvider):
    name = "null"

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        return []

    async def scrape(self, url: str) -> ScrapedPage | None:
        return None
