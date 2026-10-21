"""Smoke test for the Streamlit UI (BLUEPRINT.md §14): the script must
load and render without raising, reading from an isolated test DB. Full
interactive widget flows aren't driven here — this catches import
errors, settings/DB wiring bugs, and template mistakes in the default
(no run selected) state."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from core.config import get_settings

APP_PATH = Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py"


def test_app_loads_without_exception_with_no_run_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_API_KEY", "rsk_test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()

    at = AppTest.from_file(str(APP_PATH))
    at.run()

    assert not at.exception
    assert any("Start a new run" in info.value for info in at.info)
