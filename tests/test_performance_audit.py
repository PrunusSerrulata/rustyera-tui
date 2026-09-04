from __future__ import annotations

import io
from argparse import Namespace

import pytest

from rustyera_tui.performance_audit import (
    AuditBudget,
    AuditSpec,
    AuditWatchdog,
    ReplayVerifier,
    profiler_commands,
    wait_for_profiler_release,
)
from rustyera_tui.performance_trace import PerformanceTraceError


def test_audit_spec_rejects_shared_source_tree(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    args = Namespace(
        source_project=project,
        project=project,
        profile="emuera.skia.snake",
        scenario="fixture",
        trace=tmp_path / "missing.json",
        iterations=1,
        output=tmp_path / "result.jsonl",
        pause_at=None,
        pause_iteration=None,
    )

    with pytest.raises(PerformanceTraceError, match="must differ"):
        AuditSpec.from_args(args)


def test_profiler_plan_only_returns_headless_commands(tmp_path) -> None:
    commands = profiler_commands(123, tmp_path / "profile")

    assert [command.argv[0] for command in commands] == [
        "/usr/bin/sample",
        "/usr/bin/heap",
        "/usr/bin/vmmap",
        "/usr/bin/leaks",
        "/usr/bin/malloc_history",
    ]
    assert commands[0].argv[2:5] == ("10", "1", "-file")
    assert commands[-1].argv[-1] == "-allBySize"
    assert len({command.output for command in commands}) == len(commands)


def test_profiler_pause_fails_on_eof() -> None:
    with pytest.raises(EOFError):
        wait_for_profiler_release(io.StringIO(""))


def test_watchdog_requires_sub_five_second_interval() -> None:
    with pytest.raises(ValueError, match="below five seconds"):
        AuditWatchdog(interval_seconds=5)


def test_watchdog_rejects_identical_complete_state() -> None:
    watchdog = AuditWatchdog()
    normalized = {
        "phase": "WaitingInput",
        "wait": {"kind": "enter_key"},
        "lines": [],
        "resources": [],
        "scene": {},
        "variables": {},
        "services": [],
        "storage": [],
        "otherOutboundTags": [],
    }
    watchdog.refresh(normalized)

    with pytest.raises(TimeoutError, match="identical complete state"):
        watchdog.refresh(normalized)


def test_all_audit_phases_share_one_deadline(monkeypatch) -> None:
    moments = iter((10.0, 20.0, 30.0))
    monkeypatch.setattr("rustyera_tui.performance_audit.time.monotonic", lambda: next(moments))
    budget = AuditBudget(seconds=60)

    assert budget.require_remaining("warmup") == 50.0
    assert budget.require_remaining("profile") == 40.0


def test_replay_fails_if_runtime_idles_before_next_checkpoint() -> None:
    verifier = object.__new__(ReplayVerifier)
    verifier.trace = type("Trace", (), {"raw": {"steps": [{"checkpoint": "ready"}]}})()
    verifier.index = 0

    with pytest.raises(PerformanceTraceError, match="became idle"):
        verifier.reject_terminal_before_match("idle")
