"""Found on the first full eval sweep: Firecrawl rate-limit errors were
treated as ordinary failures, so a burst of parallel questions silently
produced zero evidence (and scored the baseline 0.0)."""

from __future__ import annotations

import pytest

import retrieval.firecrawl_client as fc
from core.errors import RetrievalError


class _Client:
    def __init__(self, failures: int, message: str) -> None:
        self.calls = 0
        self.failures, self.message = failures, message

    async def search(self, query, limit=5):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(self.message)
        return type("R", (), {"web": [type("W", (), {"url": "https://x.org/a", "title": "t"})()]})()


def _provider(client, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(fc.asyncio, "sleep", fake_sleep)
    provider = fc.FirecrawlProvider.__new__(fc.FirecrawlProvider)
    provider._client = client
    provider._semaphore = fc.asyncio.Semaphore(1)
    return provider, slept


async def test_rate_limit_is_retried_honouring_retry_after(monkeypatch):
    client = _Client(2, "Rate Limit Exceeded ... please retry after 36s, resets at")
    provider, slept = _provider(client, monkeypatch)
    results = await provider.search("q")
    assert [r.url for r in results] == ["https://x.org/a"]
    assert client.calls == 3 and slept == [38, 38]


async def test_non_rate_limit_errors_are_not_retried(monkeypatch):
    client = _Client(5, "500 internal error")
    provider, slept = _provider(client, monkeypatch)
    with pytest.raises(RetrievalError):
        await provider.search("q")
    assert client.calls == 1 and slept == []
