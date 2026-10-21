"""Enforces BLUEPRINT.md §1.2: `llm/client.py` is the only module allowed to
import `openai`, and nothing bypasses MeshClient with a raw
`.chat.completions.create(` call. This is the test that keeps the
"every LLM call routes through Mesh, schema-enforced" invariant true as the
codebase grows past Phase 1."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_OPENAI_IMPORT_FILE = ROOT / "llm" / "client.py"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".cache", "node_modules", ".pytest_cache", "tests"}


def _python_files() -> list[Path]:
    # Scans production code only. `tests/` is excluded deliberately: this file
    # itself quotes the banned pattern as a string literal to search for it,
    # and other tests mock Mesh responses that may contain it as plain text.
    return [p for p in ROOT.rglob("*.py") if not any(part in SKIP_DIRS for part in p.parts)]


def test_only_llm_client_imports_openai() -> None:
    offenders = []
    for path in _python_files():
        if path == ALLOWED_OPENAI_IMPORT_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "openai" or alias.name.startswith("openai.") for alias in node.names):
                    offenders.append(str(path))
            elif isinstance(node, ast.ImportFrom):
                if node.module and (node.module == "openai" or node.module.startswith("openai.")):
                    offenders.append(str(path))
    assert not offenders, f"only llm/client.py may import openai; offenders: {offenders}"


def test_no_raw_chat_completions_outside_client() -> None:
    offenders = []
    for path in _python_files():
        if path == ALLOWED_OPENAI_IMPORT_FILE:
            continue
        if ".chat.completions.create(" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert not offenders, f"raw Mesh/OpenAI calls must go through MeshClient.complete_json; offenders: {offenders}"
