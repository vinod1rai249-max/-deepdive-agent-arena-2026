"""Primary/secondary SearchProvider (BLUEPRINT.md §10): use the primary
(Firecrawl) until it reports that its quota/credits are exhausted, then
switch to the secondary for the rest of the process instead of returning
empty evidence for every sub-question. Transient errors are NOT a reason to
switch — only a plan/credit failure, which will not fix itself mid-run."""

from __future__ import annotations

from core.errors import RetrievalError
from core.logging import get_logger
from retrieval.base import ScrapedPage, SearchProvider, SearchResult

logger = get_logger(__name__)

_EXHAUSTED_MARKERS = ("payment required", "insufficient credits", "upgrade your plan")


def is_credit_exhausted(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _EXHAUSTED_MARKERS)


class FallbackSearchProvider(SearchProvider):
    def __init__(self, primary: SearchProvider, secondary: SearchProvider) -> None:
        self._primary, self._secondary = primary, secondary
        self._using_secondary = False

    @property
    def name(self) -> str:  # recorded on each Search row, so the trail shows which provider answered
        return self._secondary.name if self._using_secondary else self._primary.name

    def _switch(self, exc: Exception) -> None:
        if not self._using_secondary:
            logger.warning("primary search provider exhausted; switching to fallback",
                           extra={"extra_fields": {"primary": self._primary.name, "fallback": self._secondary.name}})
        self._using_secondary = True

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        if not self._using_secondary:
            try:
                return await self._primary.search(query, limit)
            except RetrievalError as exc:
                if not is_credit_exhausted(exc):
                    raise
                self._switch(exc)
        return await self._secondary.search(query, limit)

    async def scrape(self, url: str) -> ScrapedPage | None:
        if not self._using_secondary:
            try:
                return await self._primary.scrape(url)
            except RetrievalError as exc:
                if not is_credit_exhausted(exc):
                    raise
                self._switch(exc)
        return await self._secondary.scrape(url)
