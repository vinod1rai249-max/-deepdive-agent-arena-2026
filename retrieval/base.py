"""SearchProvider interface (BLUEPRINT.md §3): search + scrape only, never
LLM-extract/JSON-mode on a third-party tool's side (§1.1). Behind this
interface so the concrete provider can be swapped without touching
agents/researcher.py — and so tests can inject a fake instead of hitting
the network."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    url: str
    title: str | None = None


@dataclass
class ScrapedPage:
    url: str
    title: str | None
    text: str
    published_at: str | None = None  # raw ISO-8601 string if the provider gives one


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SearchResult]: ...

    @abstractmethod
    async def scrape(self, url: str) -> ScrapedPage | None: ...
