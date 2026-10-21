"""Found missing during a self-review (DESIGN.md §16): `core/logging.py`
had a `bind()` mechanism for attaching run_id/node context to every log
line, but nothing in agents/ or app/ ever called it — every graph node
now binds its own context fresh (app/graph.py), so this verifies the
actual mechanism those calls rely on."""

from __future__ import annotations

import json
import logging

from core.logging import JSONFormatter, bind, get_logger, unbind_all


def test_bound_context_appears_in_the_formatted_json_output():
    unbind_all()
    bind(run_id="run-123", node="verifier")

    logger = get_logger("test.logging_context")
    record = logger.makeRecord("test.logging_context", logging.INFO, __file__, 1, "something happened", (), None)

    formatted = JSONFormatter().format(record)
    payload = json.loads(formatted)

    assert payload["run_id"] == "run-123"
    assert payload["node"] == "verifier"
    assert payload["message"] == "something happened"

    unbind_all()


def test_rebinding_updates_only_the_given_keys():
    unbind_all()
    bind(run_id="run-123", node="planner")
    bind(node="researcher")  # simulates moving to the next graph node

    logger = get_logger("test.logging_context")
    record = logger.makeRecord("test.logging_context", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JSONFormatter().format(record))

    assert payload["run_id"] == "run-123"  # preserved across the re-bind
    assert payload["node"] == "researcher"  # updated

    unbind_all()


def test_unbind_all_clears_context():
    bind(run_id="run-123", node="planner")
    unbind_all()

    logger = get_logger("test.logging_context")
    record = logger.makeRecord("test.logging_context", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JSONFormatter().format(record))

    assert "run_id" not in payload
    assert "node" not in payload
