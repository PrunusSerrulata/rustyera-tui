from __future__ import annotations

import json
import os
import threading

from rustyera_tui.performance import (
    PERFORMANCE_FD_ENV,
    PerformanceProbe,
    calibrate_probe_overhead,
    install_performance_probe,
)
from rustyera_tui.startup_telemetry import emit_startup_milestone


def test_disabled_probe_does_not_append_samples() -> None:
    probe = PerformanceProbe(enabled=False)

    assert probe.start("runtime", "c_abi", "drive") is None
    assert probe.record("sample", phase="runtime", stage="c_abi", operation="drive") is False
    assert probe.snapshot() == ()


def test_probe_calibration_reports_disabled_and_counting_costs() -> None:
    result = calibrate_probe_overhead(2)

    assert result["iterations"] == 2
    assert result["offNs"] >= 0
    assert result["countingNs"] >= 0
    assert isinstance(result["overheadPercent"], float)


def test_ring_is_fixed_capacity_and_reports_drops() -> None:
    probe = PerformanceProbe(enabled=True, capacity=2)
    probe.reset({"scenario": "fixture"})

    for index in range(3):
        probe.record(
            "sample",
            phase="runtime",
            stage="fixture",
            operation=str(index),
        )

    events = probe.snapshot()
    assert len(events) == 2
    assert [event["sequence"] for event in events] == [2, 3]
    assert probe.dropped_count == 1
    assert events[-1]["droppedCount"] == 1


def test_reset_rejects_late_async_completion() -> None:
    probe = PerformanceProbe(enabled=True)
    probe.reset({"scenario": "first"})
    stale = probe.start("loading", "project", "read")

    probe.reset({"scenario": "second"})

    assert probe.finish(stale) is None
    assert probe.snapshot() == ()


def test_terminal_is_unique_across_threads_and_close_rejects_late_events() -> None:
    probe = PerformanceProbe(enabled=True)
    probe.reset()
    threads = [threading.Thread(target=probe.terminal, args=("passed",)) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    probe.close()

    assert [event["event"] for event in probe.snapshot()] == ["terminal"]
    assert probe.record("sample", phase="runtime", stage="late", operation="late") is False


def test_closed_probe_cannot_start_another_epoch() -> None:
    probe = PerformanceProbe(enabled=True)
    probe.close()

    try:
        probe.reset()
    except RuntimeError as error:
        assert "closed" in str(error)
    else:
        raise AssertionError("closed probe unexpectedly accepted reset")


def test_allocation_tracking_is_a_second_explicit_opt_in() -> None:
    baseline = PerformanceProbe(enabled=True)
    baseline.reset()
    assert baseline.allocation_checkpoint(phase="runtime", operation="baseline") is False

    probe = PerformanceProbe(enabled=True, allocation_tracking=True)
    try:
        probe.reset()
        allocation = bytearray(64)
        assert probe.allocation_checkpoint(phase="runtime", operation="fixture") is True
        assert allocation
        assert probe.snapshot()[-1]["allocator"]["kind"] == "python_tracemalloc"
    finally:
        probe.close()


def test_new_performance_fd_mirrors_startup_into_unified_schema(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    monkeypatch.setenv(PERFORMANCE_FD_ENV, str(write_fd))
    install_performance_probe(None)
    try:
        emit_startup_milestone("core_phase", phase="parse_ms", duration_ms=1.5)
        event = json.loads(os.read(read_fd, 4096))
    finally:
        install_performance_probe(None)
        os.close(read_fd)
        os.close(write_fd)

    assert event["schemaVersion"] == 2
    assert event["traceVersion"] == 1
    assert event["event"] == "core_phase"
    assert event["epoch"] == 0
    assert event["phase"] == "loading"
    assert event["stage"] == "parse_ms"
    assert event["durationNs"] == 1_500_000
    assert event["allocator"] is None
    assert event["support"]["dom"] == "not_applicable"
    assert event["support"]["canvas"] == "unsupported"
