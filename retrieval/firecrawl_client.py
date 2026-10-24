"""Firecrawl-backed SearchProvider (BLUEPRINT.md §3/§9). Search + scrape
only — no LLM-extract/JSON mode on Firecrawl's side (§1.1), since that
would be a third-party tool making an AI call on our behalf.

Exercised against a live Firecrawl account (124 real searches / 129 real
scrapes in the committed development runs). Firecrawl's optional
`check_prompt_injection` scan is deliberately NOT enabled: it is a
third-party-side content classifier, and the hackathon rule is that no
third-party tool makes AI calls on our behalf. Prompt-injection defense
lives in our own code instead — core/guardrails.py::wrap_untrusted().
"""

from __future__ import annotations

import asyncio
import re

from firecrawl.v2 import AsyncFirecrawlClient

from core.errors import RetrievalError
from retrieval.base import ScrapedPage, SearchProvider, SearchResult


_RETRY_AFTER_RE = re.compile(r"retry after (\d+)s", re.IGNORECASE)
MAX_RATE_LIMIT_RETRIES = 4
_SHARED_SEMAPHORE: asyncio.Semaphore | None = None


class FirecrawlProvider(SearchProvider):
    name = "firecrawl"

    def __init__(self, api_key: str, max_concurrency: int = 3) -> None:
        self._client = AsyncFirecrawlClient(api_key=api_key)
        # Found on the first full eval sweep: parallel questions blew through
        # Firecrawl's per-minute quota and every scrape failed, which silently
        # scored the baseline 0.0. Bound concurrency and honour retry-after.
        # Process-wide (build_deps creates one provider per run, so a
        # per-instance semaphore would not bound parallel runs).
        global _SHARED_SEMAPHORE
        if _SHARED_SEMAPHORE is None:
            _SHARED_SEMAPHORE = asyncio.Semaphore(max_concurrency)
        self._semaphore = _SHARED_SEMAPHORE

    async def _call(self, fn, *args, **kwargs):
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                async with self._semaphore:
                    return await fn(*args, **kwargs)
            except Exception as exc:
                msg = str(exc)
                if "rate limit" not in msg.lower() or attempt == MAX_RATE_LIMIT_RETRIES:
                    raise
                match = _RETRY_AFTER_RE.search(msg)
                await asyncio.sleep(min(90, (int(match.group(1)) + 2) if match else 10 * (attempt + 1)))

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        try:
            data = await self._call(self._client.search, query, limit=limit)
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
            doc = await self._call(
                self._client.scrape,
                url,
                formats=["markdown"],
                only_main_content=True,
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
