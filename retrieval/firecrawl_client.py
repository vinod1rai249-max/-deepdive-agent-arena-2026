"""Firecrawl-backed SearchProvider (BLUEPRINT.md §3/§9). Search + scrape
only — no LLM-extract/JSON mode on Firecrawl's side (§1.1), since that
would be a third-party tool making an AI call on our behalf.

NOT yet exercised against a live Firecrawl account (no FIRECRAWL_API_KEY
configured at the time this was written) — field names below were
confirmed against the installed `firecrawl-py` 4.53.2 SDK's type
definitions (`firecrawl.v2.types`), not against a real response. Verify
with a real key before trusting this in a demo.
"""

from __future__ import annotations

from firecrawl.v2 import AsyncFirecrawlClient

from core.errors import RetrievalError
from retrieval.base import ScrapedPage, SearchProvider, SearchResult


class FirecrawlProvider(SearchProvider):
    name = "firecrawl"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncFirecrawlClient(api_key=api_key)

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        try:
            data = await self._client.search(query, limit=limit)
        except Exception as exc:
            raise RetrievalError(f"Firecrawl search failed for '{query}': {exc}") from exc

        web_results = getattr(data, "web", None) or []
        return [
            SearchResult(url=r.url, title=getattr(r, "title", None))
            for r in web_results
            if getattr(r, "url", None)
        ]

    async def scrape(self, url: str) -> ScrapedPage | None:
        try:
            doc = await self._client.scrape(
                url,
                formats=["markdown"],
                only_main_content=True,
                check_prompt_injection=True,
            )
        except Exception as exc:
            raise RetrievalError(f"Firecrawl scrape failed for '{url}': {exc}") from exc

        text = getattr(doc, "markdown", None)
        if not text:
            return None

        metadata = getattr(doc, "metadata", None)
        title = getattr(metadata, "title", None) if metadata else None
        published_at = getattr(metadata, "published_time", None) if metadata else None
        return ScrapedPage(url=url, title=title, text=text, published_at=published_at)
