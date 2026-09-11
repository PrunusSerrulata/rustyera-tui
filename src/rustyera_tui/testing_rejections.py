"""Observe an expected import rejection through the existing RuntimeWorker."""

from __future__ import annotations

import copy
import json
import queue
import time
from pathlib import Path
from typing import Any

from .testing_support import TestDriverError


def reject_snapshot(
    session: Any, path: Path, expected: dict[str, Any], deadline: float
) -> dict[str, Any]:
    from .testing import TERMINAL_EVENTS, apply_presentation_event

    client = session.worker.client
    if client is None or client.active_wait is None:
        raise TestDriverError("snapshot rejection requires an active runtime wait")
    before = copy.deepcopy((client.phase, client.epoch, client.active_wait))
    session.restore_snapshot(path)
    rejection = None
    paired_error = False
    presentation_retired = False
    while time.monotonic() < deadline:
        try:
            event = session.worker.events.get(
                timeout=min(0.25, max(0, deadline - time.monotonic()))
            )
        except queue.Empty:
            if not session.worker.is_alive():
                raise TestDriverError("runtime worker stopped during snapshot rejection")
            continue
        if session._acknowledge_frontend_boundary(event):
            continue
        delivered_wait = apply_presentation_event(session.model, event)
        presentation_retired = presentation_retired or event.kind == "game_state_reset"
        if event.kind == "command_rejected":
            if rejection is not None or event.value.get("import_purpose") not in {
                "snapshot",
                "game_state",
            }:
                raise TestDriverError("unexpected additional or unrelated rejection")
            rejection = event.value
            # JSON normalizes CBOR integer map keys to the scenario's JSON schema.
            actual = {"code": rejection.get("code"), "context": rejection.get("context")}
            if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise TestDriverError(f"snapshot rejection differs: {actual!r}")
        elif event.kind == "runtime_error" and rejection is not None:
            if event.value != rejection["error_message"]:
                raise TestDriverError(f"unrelated runtime error: {event.value}")
            paired_error = True
        elif event.kind in TERMINAL_EVENTS:
            raise TestDriverError(f"{event.kind}: {event.value}")
        elif event.kind == "status":
            session.statuses.append(str(event.value))
        elif event.kind == "log":
            session.logs.append(str(event.value))
        if paired_error and (not presentation_retired or delivered_wait is not None):
            after = (client.phase, client.epoch, client.active_wait)
            if before != after:
                raise TestDriverError(
                    "rejected snapshot changed the active runtime wait or identity"
                )
            if client.pending_import is not None or client.pending_restore is not None:
                raise TestDriverError("rejected snapshot retained import resources")
            return {
                "rejection": rejection,
                "wait_preserved": True,
                "observation": session._observation(client.active_wait),
            }
    raise TestDriverError("timed out waiting for the expected snapshot rejection")


def check_compiled_cache_expectation(
    expectation: str, supplied: bool, reports: list[dict[str, Any]], logs: list[str]
) -> None:
    if expectation != "source_fallback":
        if supplied and not any("runtime.compiled_cache_hit" in log for log in logs):
            raise TestDriverError("cross-host compiled cache was not accepted")
        return
    if not supplied:
        raise TestDriverError("source fallback requires an explicitly supplied compiled cache")
    requested = None
    completed = False
    for observation in reports:
        report = observation.get("report", {})
        diagnostics = report.get(2, [])
        if any(item.get(0) == "runtime.compiled_cache_hit" for item in diagnostics):
            raise TestDriverError("compiled cache was accepted instead of rejected")
        if (
            type(observation.get("correlation_id")) is int
            and report.get(3) is True
            and any(item.get(0) == "runtime.compiled_cache_ignored" for item in diagnostics)
        ):
            requested = report.get(0)
        elif (
            requested is not None
            and report.get(0) == requested
            and type(observation.get("correlation_id")) is int
            and report.get(1) is True
            and report.get(3, False) is False
        ):
            completed = True
    if not completed:
        raise TestDriverError(
            "missing correlated cache rejection, source request, or successful source report"
        )
