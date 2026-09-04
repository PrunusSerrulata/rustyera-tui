"""Strictly opt-in loading and runtime performance telemetry.

This module is the single timing, buffering, and JSONL authority for the TUI.  Normal
application runs use :data:`DISABLED_PROBE`; callers on hot paths must test ``enabled``
before collecting details or allocating metric dictionaries.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import tracemalloc
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no POSIX resource module.
    resource = None  # type: ignore[assignment]

PERFORMANCE_SCHEMA_VERSION = 2
PERFORMANCE_TRACE_VERSION = 1
PERFORMANCE_FD_ENV = "RUSTYERA_PERF_FD"
PERFORMANCE_ALLOCATIONS_ENV = "RUSTYERA_PERF_ALLOCATIONS"
LEGACY_STARTUP_FD_ENV = "RUSTYERA_STARTUP_TELEMETRY_FD"
DEFAULT_RING_CAPACITY = 4096


@dataclass(frozen=True, slots=True)
class SpanToken:
    """A timer tied to one audit epoch so late async work can be discarded."""

    audit_epoch: int
    started_ns: int
    phase: str
    stage: str
    operation: str


class _JsonlFdSink:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        fpathconf = getattr(os, "fpathconf", None)
        try:
            self.atomic_bytes = fpathconf(fd, "PC_PIPE_BUF") if fpathconf else 512
        except (OSError, ValueError):
            self.atomic_bytes = 512

    @classmethod
    def from_environment(cls) -> _JsonlFdSink | None:
        raw = os.environ.get(PERFORMANCE_FD_ENV)
        if raw is None:
            return None
        try:
            fd = int(raw)
            if os.get_blocking(fd) or not stat.S_ISFIFO(os.fstat(fd).st_mode):
                return None
        except (OSError, TypeError, ValueError):
            return None
        return cls(fd)

    def write(self, event: Mapping[str, Any]) -> bool:
        try:
            encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode()
            if len(encoded) > self.atomic_bytes:
                return False
            os.write(self.fd, encoded)
            return True
        except (OSError, TypeError, ValueError):
            return False


class PerformanceProbe:
    """Bounded, epoch-aware event recorder shared by loading and runtime paths."""

    def __init__(
        self,
        *,
        enabled: bool,
        capacity: int = DEFAULT_RING_CAPACITY,
        sink: _JsonlFdSink | None = None,
        allocation_tracking: bool = False,
    ) -> None:
        if capacity < 1:
            raise ValueError("performance ring capacity must be positive")
        self.enabled = enabled
        self._capacity = capacity
        self._sink = sink if enabled else None
        self._events: deque[dict[str, Any]] | None = (
            deque(maxlen=capacity) if enabled else None
        )
        self._lock: threading.Lock | None = threading.Lock() if enabled else None
        self._audit_epoch = 0
        self._sequence = 0
        self._dropped_count = 0
        self._identity: Mapping[str, Any] | None = None
        self._terminal_emitted = False
        self._closed = False
        self._allocation_tracking = enabled and allocation_tracking
        self._allocation_snapshot: tracemalloc.Snapshot | None = None
        self._owns_tracemalloc = False
        if self._allocation_tracking and not tracemalloc.is_tracing():
            tracemalloc.start()
            self._owns_tracemalloc = True

    @classmethod
    def from_environment(cls, *, capacity: int = DEFAULT_RING_CAPACITY) -> PerformanceProbe:
        sink = _JsonlFdSink.from_environment()
        return (
            cls(
                enabled=True,
                capacity=capacity,
                sink=sink,
                allocation_tracking=os.environ.get(PERFORMANCE_ALLOCATIONS_ENV) == "1",
            )
            if sink is not None
            else DISABLED_PROBE
        )

    @property
    def audit_epoch(self) -> int:
        return self._audit_epoch

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def reset(self, identity: Mapping[str, Any] | None = None) -> int:
        """Begin a new epoch and make all earlier async span tokens stale."""

        if not self.enabled:
            return 0
        assert self._lock is not None and self._events is not None
        with self._lock:
            if self._closed:
                raise RuntimeError("closed performance probe cannot be reset")
            self._audit_epoch += 1
            self._sequence = 0
            self._dropped_count = 0
            self._events.clear()
            self._identity = dict(identity) if identity is not None else None
            self._terminal_emitted = False
            if self._allocation_tracking:
                tracemalloc.reset_peak()
                self._allocation_snapshot = tracemalloc.take_snapshot()
            return self._audit_epoch

    def start(self, phase: str, stage: str, operation: str) -> SpanToken | None:
        if not self.enabled:
            return None
        assert self._lock is not None
        with self._lock:
            if self._closed:
                return None
            epoch = self._audit_epoch
        return SpanToken(epoch, time.monotonic_ns(), phase, stage, operation)

    def finish(
        self,
        token: SpanToken | None,
        *,
        event: str = "sample",
        fields: Mapping[str, Any] | None = None,
    ) -> int | None:
        if token is None or not self.enabled or token.audit_epoch != self._audit_epoch:
            return None
        duration_ns = time.monotonic_ns() - token.started_ns
        self.record(
            event,
            phase=token.phase,
            stage=token.stage,
            operation=token.operation,
            duration_ns=duration_ns,
            fields=fields,
            expected_epoch=token.audit_epoch,
        )
        return duration_ns

    def record(
        self,
        event: str,
        *,
        phase: str,
        stage: str,
        operation: str,
        duration_ns: int | None = None,
        fields: Mapping[str, Any] | None = None,
        expected_epoch: int | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        assert self._lock is not None and self._events is not None
        with self._lock:
            if self._closed or (
                expected_epoch is not None and expected_epoch != self._audit_epoch
            ):
                return False
            return self._record_locked(
                event,
                phase=phase,
                stage=stage,
                operation=operation,
                duration_ns=duration_ns,
                fields=fields,
            )

    def _record_locked(
        self,
        event: str,
        *,
        phase: str,
        stage: str,
        operation: str,
        duration_ns: int | None,
        fields: Mapping[str, Any] | None,
    ) -> bool:
        """Record while holding ``_lock``; never call from a disabled probe."""

        assert self._events is not None
        self._sequence += 1
        payload: dict[str, Any] = {
                "schemaVersion": PERFORMANCE_SCHEMA_VERSION,
                "traceVersion": PERFORMANCE_TRACE_VERSION,
                "epoch": self._audit_epoch,
                "sequence": self._sequence,
                "event": event,
                "client": "tui",
                "phase": phase,
                "stage": stage,
                "operation": operation,
                "monotonicNs": time.monotonic_ns(),
                "durationNs": duration_ns,
                "droppedCount": self._dropped_count,
                "identity": self._identity,
                "bytes": None,
                "count": None,
                "vmInstructions": None,
                "runtimeTransitions": None,
                "envelopeCount": None,
                "envelopeBytes": None,
                "snapshotCount": None,
                "deltaCount": None,
                "lineCount": None,
                "runCount": None,
                "resourceCount": None,
                "sceneLayerCount": None,
                "memory": None,
                "allocator": None,
                "support": {
                    "dom": "not_applicable",
                    "canvas": "unsupported",
                    "nextPaint": "not_applicable",
                },
        }
        if fields is not None:
            for key, value in fields.items():
                if key not in {
                        "schemaVersion",
                        "traceVersion",
                        "epoch",
                        "sequence",
                        "event",
                        "client",
                        "phase",
                        "stage",
                        "operation",
                }:
                    payload[key] = value
        if len(self._events) == self._capacity:
            self._dropped_count += 1
            payload["droppedCount"] = self._dropped_count
        self._events.append(payload)
        if self._sink is not None and not self._sink.write(payload):
            self._dropped_count += 1
        return True

    def terminal(self, status: str, *, error: str | None = None) -> bool:
        if status not in {"passed", "failed"}:
            raise ValueError("performance terminal status must be passed or failed")
        if not self.enabled:
            return False
        assert self._lock is not None
        with self._lock:
            if self._closed or self._terminal_emitted:
                return False
            self._terminal_emitted = True
            return self._record_locked(
                "terminal",
                phase="audit",
                stage="terminal",
                operation="finish",
                duration_ns=None,
                fields={"status": status, "error": error},
            )

    def memory_checkpoint(self, *, phase: str, operation: str) -> bool:
        """Sample memory explicitly at a low-frequency audit boundary."""

        if not self.enabled:
            return False
        return self.record(
            "memory",
            phase=phase,
            stage="memory",
            operation=operation,
            fields={"memory": self._memory_fields()},
        )

    def allocation_checkpoint(self, *, phase: str, operation: str) -> bool:
        """Collect one low-frequency Python allocation diff in allocation rounds only."""

        if not self.enabled or not self._allocation_tracking:
            return False
        assert self._lock is not None
        with self._lock:
            if self._closed:
                return False
            baseline = self._allocation_snapshot
            current, peak = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            diff_count = 0
            diff_bytes = 0
            if baseline is not None:
                for statistic in snapshot.compare_to(baseline, "traceback"):
                    diff_count += statistic.count_diff
                    diff_bytes += statistic.size_diff
            self._allocation_snapshot = snapshot
            return self._record_locked(
                "allocation",
                phase=phase,
                stage="memory",
                operation=operation,
                duration_ns=None,
                fields={
                    "allocator": {
                        "kind": "python_tracemalloc",
                        "currentBytes": current,
                        "peakBytes": peak,
                        "diffCount": diff_count,
                        "diffBytes": diff_bytes,
                    }
                },
            )

    def close(self) -> None:
        """Release an explicitly enabled Python allocation tracker."""

        if not self.enabled:
            return
        assert self._lock is not None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._allocation_snapshot = None
            owns_tracemalloc = self._owns_tracemalloc
            self._owns_tracemalloc = False
        if owns_tracemalloc and tracemalloc.is_tracing():
            tracemalloc.stop()

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        if not self.enabled:
            return ()
        assert self._lock is not None and self._events is not None
        with self._lock:
            return tuple(dict(event) for event in self._events)

    def drain(self) -> tuple[dict[str, Any], ...]:
        """Remove buffered events after a runner has durably appended them."""

        if not self.enabled:
            return ()
        assert self._lock is not None and self._events is not None
        with self._lock:
            events = tuple(dict(event) for event in self._events)
            self._events.clear()
            return events

    @staticmethod
    def _memory_fields() -> dict[str, int | None]:
        if resource is None:
            return {"rssBytes": None, "peakRssBytes": None}
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        return {"rssBytes": None, "peakRssBytes": peak_bytes}


DISABLED_PROBE = PerformanceProbe(enabled=False)
_global_probe: PerformanceProbe | None = None
_global_lock = threading.Lock()


def performance_probe() -> PerformanceProbe:
    """Return the process probe, resolving opt-in configuration only once."""

    global _global_probe
    if _global_probe is None:
        with _global_lock:
            if _global_probe is None:
                _global_probe = PerformanceProbe.from_environment()
    return _global_probe


def install_performance_probe(probe: PerformanceProbe | None) -> None:
    """Install an explicit probe for an audit runner or a unit test."""

    global _global_probe
    with _global_lock:
        _global_probe = probe


def calibrate_probe_overhead(iterations: int = 10_000) -> dict[str, int | float]:
    """Measure disabled-branch versus in-memory event cost for audit metadata."""

    if iterations < 1:
        raise ValueError("performance calibration iterations must be positive")
    disabled_started = time.perf_counter_ns()
    for _ in range(iterations):
        DISABLED_PROBE.record("sample", phase="audit", stage="probe", operation="calibrate")
    off_ns = time.perf_counter_ns() - disabled_started
    counting = PerformanceProbe(enabled=True, capacity=min(iterations, DEFAULT_RING_CAPACITY))
    counting_started = time.perf_counter_ns()
    for _ in range(iterations):
        counting.record("sample", phase="audit", stage="probe", operation="calibrate")
    counting_ns = time.perf_counter_ns() - counting_started
    overhead = ((counting_ns - off_ns) / off_ns * 100) if off_ns else 0.0
    return {
        "iterations": iterations,
        "offNs": off_ns,
        "countingNs": counting_ns,
        "overheadPercent": overhead,
    }
