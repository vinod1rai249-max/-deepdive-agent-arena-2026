"""FreeWebProvider (Wikipedia/arXiv/PubMed, mocked at the HTTP layer) and the
Firecrawl -> free_web credit-exhaustion fallback (BLUEPRINT.md §10)."""

from __future__ import annotations

import httpx
import pytest
import respx

from core.errors import RetrievalError
from retrieval.base import ScrapedPage, SearchProvider, SearchResult
from retrieval.fallback_provider import FallbackSearchProvider, is_credit_exhausted
from retrieval.free_web_provider import ARXIV_API, EUTILS, WIKI_API, FreeWebProvider

ARXIV_XML = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2401.00001v1</id><title>Agentic  RAG
 survey</title><summary>We survey agentic retrieval.</summary><published>2024-01-01T00:00:00Z</published>
</entry></feed>"""


@respx.mock
async def test_search_merges_sources_and_survives_one_being_down():
    respx.get(WIKI_API).mock(return_value=httpx.Response(200, json={"query": {"search": [{"title": "Lithium-ion battery"}]}}))
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_XML))
    respx.get(f"{EUTILS}/esearch.fcgi").mock(return_value=httpx.Response(500))

    results = await FreeWebProvider().search("lithium battery", limit=4)
    urls = [r.url for r in results]
    assert "https://en.wikipedia.org/wiki/Lithium-ion_battery" in urls
    assert "https://arxiv.org/abs/2401.00001v1" in urls  # PubMed failing did not fail the search


@respx.mock
async def test_scrape_wikipedia_returns_plain_text_with_title():
    respx.get(WIKI_API).mock(
        return_value=httpx.Response(200, json={"query": {"pages": {"1": {"title": "Lithium-ion battery", "extract": "A lithium-ion battery is rechargeable."}}}})
    )
    page = await FreeWebProvider().scrape("https://en.wikipedia.org/wiki/Lithium-ion_battery")
    assert page and page.title == "Lithium-ion battery" and "rechargeable" in page.text


@respx.mock
async def test_scrape_arxiv_uses_the_abstract():
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_XML))
    page = await FreeWebProvider().scrape("https://arxiv.org/abs/2401.00001v1")
    assert page and "We survey agentic retrieval." in page.text and page.published_at.startswith("2024")


async def test_generic_scrape_blocks_private_addresses():
    with pytest.raises(RetrievalError):
        await FreeWebProvider().scrape("http://127.0.0.1:8080/secret")


class _Stub(SearchProvider):
    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name, self.error, self.calls = name, error, 0

    async def search(self, query, limit=5):
        self.calls += 1
        if self.error:
            raise self.error
        return [SearchResult(url=f"https://{self.name}.test/a")]

    async def scrape(self, url):
        self.calls += 1
        if self.error:
            raise self.error
        return ScrapedPage(url=url, title=None, text="x")


async def test_credit_exhaustion_switches_to_secondary_permanently():
    primary = _Stub("firecrawl", RetrievalError("Payment Required: Insufficient credits"))
    secondary = _Stub("free_web")
    provider = FallbackSearchProvider(primary, secondary)

    assert provider.name == "firecrawl"
    results = await provider.search("q")
    assert results[0].url.startswith("https://free_web") and provider.name == "free_web"
    await provider.search("q again")
    assert primary.calls == 1  # not retried once it is known to be exhausted


async def test_transient_error_does_not_switch():
    provider = FallbackSearchProvider(_Stub("firecrawl", RetrievalError("timeout")), _Stub("free_web"))
    with pytest.raises(RetrievalError):
        await provider.search("q")
    assert provider.name == "firecrawl"


def test_is_credit_exhausted_markers():
    assert is_credit_exhausted(RetrievalError("Payment Required: Failed to scrape"))
    assert not is_credit_exhausted(RetrievalError("Rate Limit Exceeded"))
