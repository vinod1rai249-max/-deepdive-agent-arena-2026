"""Free, key-less SearchProvider (BLUEPRINT.md §10 "second provider as a
fallback"): Wikipedia + arXiv + PubMed through their public APIs, plus a
plain safe HTTP fetch for any other URL.

Why it exists: Firecrawl is the primary provider, but its free credits ran
out mid-evaluation ("Payment Required") and the next reset is after the
submission deadline. Rather than leave the system with zero evidence, the
interface's second provider takes over. It is search + scrape only — no AI
call on any third party's side (BLUEPRINT.md §1.1).

Honest limits: Wikipedia/arXiv/PubMed are good for background, science and
health, and weak for very recent news, paywalled outlets and 2026-specific
market data. Evaluation numbers produced with this provider are labelled as
such and are not directly comparable to Firecrawl-backed runs.
"""

from __future__ import annotations

import asyncio
import io
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

import httpx

from core.errors import GuardrailViolation, RetrievalError
from retrieval.base import ScrapedPage, SearchProvider, SearchResult
from retrieval.fetch_safe import assert_safe_url

USER_AGENT = "DeepDiveResearchBot/0.1 (hackathon research project; https://github.com/vinod1rai249-max)"
WIKI_API = "https://en.wikipedia.org/w/api.php"
ARXIV_API = "https://export.arxiv.org/api/query"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 60_000
_ATOM = {"a": "http://www.w3.org/2005/Atom"}


class FreeWebProvider(SearchProvider):
    name = "free_web"

    def __init__(self, timeout: float = 20.0, max_concurrency: int = 4) -> None:
        self._http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _get(self, url: str, **params) -> httpx.Response:
        async with self._semaphore:
            try:
                resp = await self._http.get(url, params=params or None)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                raise RetrievalError(f"free_web request failed for {url}: {exc}") from exc

    # ---- search ---------------------------------------------------------

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        per_source = max(1, limit // 2 + limit % 2)
        wiki, arxiv, pubmed = await asyncio.gather(
            self._search_wikipedia(query, per_source),
            self._search_arxiv(query, 2),
            self._search_pubmed(query, 2),
            return_exceptions=True,
        )
        results: list[SearchResult] = []
        for group in (wiki, pubmed, arxiv):
            if isinstance(group, BaseException):
                continue  # one source down must not fail the search
            results.extend(group)
        if not results and all(isinstance(g, BaseException) for g in (wiki, arxiv, pubmed)):
            raise RetrievalError(f"free_web search failed for '{query}': {wiki}")
        return results[:limit + 2]

    async def _search_wikipedia(self, query: str, n: int) -> list[SearchResult]:
        data = (await self._get(WIKI_API, action="query", list="search", srsearch=query, srlimit=n, format="json")).json()
        return [
            SearchResult(url=f"https://en.wikipedia.org/wiki/{quote(h['title'].replace(' ', '_'))}", title=h["title"])
            for h in data.get("query", {}).get("search", [])
        ]

    async def _search_arxiv(self, query: str, n: int) -> list[SearchResult]:
        root = ET.fromstring((await self._get(ARXIV_API, search_query=f"all:{query}", max_results=n)).text)
        out = []
        for entry in root.findall("a:entry", _ATOM):
            link = (entry.findtext("a:id", default="", namespaces=_ATOM) or "").strip()
            if link:
                out.append(SearchResult(url=link.replace("http://", "https://"), title=" ".join((entry.findtext("a:title", default="", namespaces=_ATOM)).split())))
        return out

    async def _search_pubmed(self, query: str, n: int) -> list[SearchResult]:
        data = (await self._get(f"{EUTILS}/esearch.fcgi", db="pubmed", term=query, retmax=n, retmode="json")).json()
        return [SearchResult(url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/", title=None) for pid in data.get("esearchresult", {}).get("idlist", [])]

    # ---- scrape ---------------------------------------------------------

    async def scrape(self, url: str) -> ScrapedPage | None:
        host = (urlparse(url).hostname or "").lower()
        if host.endswith("wikipedia.org"):
            return await self._scrape_wikipedia(url)
        if host == "arxiv.org":
            return await self._scrape_arxiv(url)
        if host == "pubmed.ncbi.nlm.nih.gov":
            return await self._scrape_pubmed(url)
        return await self._scrape_generic(url)

    async def _scrape_wikipedia(self, url: str) -> ScrapedPage | None:
        title = unquote(urlparse(url).path.rsplit("/", 1)[-1]).replace("_", " ")
        data = (await self._get(WIKI_API, action="query", prop="extracts", explaintext=1, redirects=1, titles=title, format="json")).json()
        pages = data.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        text = (page.get("extract") or "").strip()
        return ScrapedPage(url=url, title=page.get("title"), text=text[:MAX_TEXT_CHARS]) if text else None

    async def _scrape_arxiv(self, url: str) -> ScrapedPage | None:
        arxiv_id = urlparse(url).path.replace("/abs/", "").strip("/")
        root = ET.fromstring((await self._get(ARXIV_API, id_list=arxiv_id)).text)
        entry = root.find("a:entry", _ATOM)
        if entry is None:
            return None
        title = " ".join((entry.findtext("a:title", default="", namespaces=_ATOM)).split())
        summary = " ".join((entry.findtext("a:summary", default="", namespaces=_ATOM)).split())
        published = entry.findtext("a:published", default=None, namespaces=_ATOM)
        return ScrapedPage(url=url, title=title, text=f"{title}\n\n{summary}", published_at=published) if summary else None

    async def _scrape_pubmed(self, url: str) -> ScrapedPage | None:
        pmid = urlparse(url).path.strip("/")
        root = ET.fromstring((await self._get(f"{EUTILS}/efetch.fcgi", db="pubmed", id=pmid, retmode="xml")).text)
        art = root.find(".//PubmedArticle")
        if art is None:
            return None
        title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else None
        abstract = "\n".join("".join(t.itertext()) for t in art.findall(".//AbstractText"))
        return ScrapedPage(url=url, title=title, text=f"{title}\n\n{abstract}") if abstract.strip() else None

    async def _scrape_generic(self, url: str) -> ScrapedPage | None:
        """Plain fetch for arbitrary URLs (used mainly to score the baseline's
        LLM-authored citations). SSRF-guarded; redirects re-checked; body capped."""
        current = url
        for _ in range(4):
            try:
                assert_safe_url(current)
            except GuardrailViolation as exc:
                raise RetrievalError(str(exc)) from exc
            async with self._semaphore:
                try:
                    resp = await self._http.get(current)
                except httpx.HTTPError as exc:
                    raise RetrievalError(f"fetch failed for {current}: {exc}") from exc
            if resp.is_redirect and resp.headers.get("location"):
                current = str(httpx.URL(current).join(resp.headers["location"]))
                continue
            if resp.status_code >= 400:
                raise RetrievalError(f"fetch failed for {current}: HTTP {resp.status_code}")
            body = resp.content[:MAX_BODY_BYTES]
            return await asyncio.to_thread(self._to_text, current, body, resp.headers.get("content-type", ""))
        raise RetrievalError(f"too many redirects for {url}")

    @staticmethod
    def _to_text(url: str, body: bytes, content_type: str) -> ScrapedPage | None:
        from markitdown import MarkItDown

        ext = ".pdf" if "pdf" in content_type else ".html"
        try:
            text = MarkItDown().convert_stream(io.BytesIO(body), file_extension=ext).text_content
        except Exception as exc:  # unparseable body is a failed fetch, not a crash
            raise RetrievalError(f"could not parse {url}: {exc}") from exc
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ScrapedPage(url=url, title=None, text=text[:MAX_TEXT_CHARS]) if text else None
