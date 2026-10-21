"""Shared collaborators injected into every graph node, kept as one small
container so nodes stay functions of (state, deps) and tests can swap in
fakes (a FakeSearchProvider, an in-memory Repo) without touching node
code."""

from __future__ import annotations

from dataclasses import dataclass

from core.config import Settings
from llm.client import MeshClient
from retrieval.base import SearchProvider
from store.repo import Repo


@dataclass
class Deps:
    mesh: MeshClient
    search: SearchProvider
    repo: Repo
    settings: Settings
