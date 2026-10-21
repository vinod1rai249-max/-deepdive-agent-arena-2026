"""Disk cache for Mesh responses, keyed by (model, prompt_version, prompt
hash) per BLUEPRINT.md §8/§11. Makes reruns during development free and
avoids re-spending credits on identical calls."""

from __future__ import annotations

import hashlib
from pathlib import Path


class DiskCache:
    def __init__(self, directory: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.dir = Path(directory)
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(model: str, prompt_version: str, rendered_prompt: str) -> str:
        return hashlib.sha256(f"{model}|{prompt_version}|{rendered_prompt}".encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> str | None:
        if not self.enabled:
            return None
        path = self.dir / f"{cache_key}.json"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def set(self, cache_key: str, raw_json: str) -> None:
        if not self.enabled:
            return
        path = self.dir / f"{cache_key}.json"
        path.write_text(raw_json, encoding="utf-8")
