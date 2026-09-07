"""Supervisor and child-round entry points for headless performance replay."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

from .performance import DISABLED_PROBE, PerformanceProbe, install_performance_probe
from .performance_process import (
    ProcessIdentity,
    collect_profiler_artifacts,
    publish_json_exclusive,
)
from .performance_replay import AuditWatchdog, ReplayVerifier, replay_once, write_probe_events
from .performance_trace import PerformanceTrace, PerformanceTraceError


def run_child_round(
    *,
    project: Path,
    trace_path: Path,
    scenario: str,
    runtime_library: Path | None,
    phase: str,
    iteration: int | None,
    output: Path,
    checkpoint_manifest: Path | None,
    pause_at: str | None,
    allocation_tracking: bool,
    deadline: float,
) -> None:
    """Execute one audited child; the supervisor owns this process and its stdin."""

    trace = PerformanceTrace.load(trace_path, scenario=scenario)
    probe = PerformanceProbe(enabled=True, allocation_tracking=allocation_tracking)
    # Scanner, worker, client, and frontend helpers all resolve this one process authority.
    install_performance_probe(probe)
    probe.reset(
        {
            "scenario": scenario,
            "projectDigest": trace.raw["projectDigest"],
            "traceDigest": trace.raw["traceDigest"],
            "round": phase,
            "iteration": iteration,
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        def pause_flush(session: Any, step: dict[str, Any]) -> None:
            write_probe_events(stream, probe)
            stream.flush()
            os.fsync(stream.fileno())
            if checkpoint_manifest is None:
                raise RuntimeError("pause round lacks a checkpoint manifest target")
            client = session.worker.client
            if client is None:
                raise RuntimeError("runtime client disappeared at profiler checkpoint")
            identity = ProcessIdentity.capture(os.getpid())
            publish_json_exclusive(
                checkpoint_manifest,
                {
                    "schemaVersion": 1,
                    "checkpoint": step["checkpoint"],
                    "phase": phase,
                    "iteration": iteration,
                    "pid": identity.pid,
                    "pgid": identity.pgid,
                    "started": identity.started,
                    "command": identity.command,
                    "session": {
                        "id": client.session,
                        "epoch": client.epoch,
                        "runtimePhase": client.phase,
                    },
                },
            )

        try:
            replay_once(
                trace=trace,
                project=project,
                runtime_library=runtime_library,
                probe=probe,
                verifier=ReplayVerifier(trace),
                watchdog=AuditWatchdog(),
                deadline=deadline,
                pause_at=pause_at,
                pause_release=sys.stdin.buffer if pause_at is not None else None,
                pause_flush=pause_flush if pause_at is not None else None,
            )
            if allocation_tracking:
                probe.allocation_checkpoint(phase="audit", operation="round_complete")
            probe.memory_checkpoint(phase="audit", operation="round_complete")
            probe.terminal("passed")
        except BaseException as error:
            probe.terminal("failed", error=str(error))
            raise
        finally:
            probe.close()
            write_probe_events(stream, probe)
            os.fsync(stream.fileno())
            install_performance_probe(DISABLED_PROBE)


def _child_argv(
    spec: Any,
    *,
    phase: str,
    iteration: int | None,
    output: Path,
    checkpoint_manifest: Path | None,
    allocation_tracking: bool,
    deadline: float,
    runtime_library: Path | None,
    profile_checkpoint: str,
) -> tuple[str, ...]:
    script = Path(__file__).resolve().parents[2] / "tools" / "performance-audit.py"
    argv = [
        sys.executable,
        str(script),
        "_round",
        "--project",
        str(spec.project),
        "--trace",
        str(spec.trace),
        "--scenario",
        spec.scenario,
        "--phase",
        phase,
        "--output",
        str(output),
        "--deadline",
        repr(deadline),
    ]
    if iteration is not None:
        argv.extend(("--iteration", str(iteration)))
    if checkpoint_manifest is not None:
        argv.extend(("--checkpoint-manifest", str(checkpoint_manifest)))
    if phase in {"cpu_profile", "allocation_profile"}:
        argv.extend(("--pause-at", profile_checkpoint))
    if allocation_tracking:
        argv.append("--allocation-tracking")
    if runtime_library is not None:
        argv.extend(("--runtime-library", str(runtime_library)))
    return tuple(argv)


def _append_file(source: Path, destination: BinaryIO) -> None:
    with source.open("rb") as stream:
        shutil.copyfileobj(stream, destination, 1024 * 1024)
    destination.flush()
    os.fsync(destination.fileno())


def _write_supervisor_failure(
    destination: BinaryIO, *, phase: str, iteration: int | None, error: str
) -> None:
    probe = PerformanceProbe(enabled=True)
    probe.reset({"round": phase, "iteration": iteration, "authority": "supervisor"})
    probe.terminal("failed", error=error)
    probe.close()
    write_probe_events(destination, probe)
    os.fsync(destination.fileno())


def run_supervised(spec: Any, budget: Any, *, runtime_library: Path | None) -> None:
    """Run every round as an identity-tracked child under one shared deadline."""

    process_manifest = spec.output.with_suffix(spec.output.suffix + ".processes.json")
    if process_manifest.exists():
        raise FileExistsError(process_manifest)
    history: list[dict[str, Any]] = []
    trace = PerformanceTrace.load(spec.trace, scenario=spec.scenario)
    profile_checkpoint = spec.pause_at or trace.raw["steps"][0]["checkpoint"]
    rounds = [
        ("warmup", None, False),
        *[("baseline", index, False) for index in range(spec.iterations)],
        ("cpu_profile", None, False),
        ("allocation_profile", None, True),
    ]
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{spec.output.name}.rounds.", dir=spec.output.parent
        ) as temporary_name, spec.output.open("xb") as combined:
            temporary = Path(temporary_name)
            for ordinal, (phase, iteration, allocation_tracking) in enumerate(rounds):
                remaining = budget.require_remaining(f"{phase}:{iteration}")
                round_output = temporary / f"{ordinal:03d}-{phase}.jsonl"
                checkpoint = temporary / f"{ordinal:03d}-{phase}.checkpoint.json"
                stderr_path = temporary / f"{ordinal:03d}-{phase}.stderr.txt"
                with stderr_path.open("xb") as stderr:
                    owned = budget.processes.spawn(
                        _child_argv(
                            spec,
                            phase=phase,
                            iteration=iteration,
                            output=round_output,
                            checkpoint_manifest=checkpoint,
                            allocation_tracking=allocation_tracking,
                            deadline=budget.deadline,
                            runtime_library=runtime_library,
                            profile_checkpoint=profile_checkpoint,
                        ),
                        allocation_round=allocation_tracking,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr,
                    )
                    descendants = budget.processes.descendants(owned.identity.pid)
                    record = {
                        "phase": phase,
                        "iteration": iteration,
                        "allocationTracking": allocation_tracking,
                        "process": {
                            "pid": owned.identity.pid,
                            "pgid": owned.identity.pgid,
                            "started": owned.identity.started,
                            "command": owned.identity.command,
                        },
                        "descendants": [
                            {
                                "pid": item.pid,
                                "pgid": item.pgid,
                                "started": item.started,
                                "command": item.command,
                            }
                            for item in descendants
                        ],
                    }
                    history.append(record)
                    profiled = False
                    while owned.process.poll() is None:
                        if budget.remaining() <= 0:
                            message = f"shared audit deadline expired in {phase}"
                            budget.incomplete_phases.append(phase)
                            _write_supervisor_failure(
                                combined,
                                phase=phase,
                                iteration=iteration,
                                error=message,
                            )
                            raise TimeoutError(message)
                        if checkpoint.exists() and not profiled:
                            identity = ProcessIdentity.capture(owned.identity.pid)
                            if identity != owned.identity:
                                raise RuntimeError("checkpoint PID identity differs from child")
                            checkpoint_value = json.loads(checkpoint.read_text(encoding="utf-8"))
                            if (
                                not isinstance(checkpoint_value, dict)
                                or set(checkpoint_value)
                                != {
                                    "schemaVersion",
                                    "checkpoint",
                                    "phase",
                                    "iteration",
                                    "pid",
                                    "pgid",
                                    "started",
                                    "command",
                                    "session",
                                }
                                or checkpoint_value.get("schemaVersion") != 1
                                or checkpoint_value.get("pid") != owned.identity.pid
                                or checkpoint_value.get("pgid") != owned.identity.pgid
                                or checkpoint_value.get("started") != owned.identity.started
                                or checkpoint_value.get("command") != owned.identity.command
                                or checkpoint_value.get("phase") != phase
                                or checkpoint_value.get("iteration") != iteration
                                or not isinstance(checkpoint_value.get("session"), dict)
                            ):
                                raise RuntimeError("checkpoint manifest identity mismatch")
                            record["descendants"] = [
                                {
                                    "pid": item.pid,
                                    "pgid": item.pgid,
                                    "started": item.started,
                                    "command": item.command,
                                }
                                for item in budget.processes.descendants(owned.identity.pid)
                            ]
                            record["checkpoint"] = checkpoint_value
                            record["profiles"] = collect_profiler_artifacts(
                                owned.identity,
                                spec.output.with_name(f"{spec.output.stem}.{phase}"),
                                deadline=budget.deadline,
                                include=(
                                    ("heap", "vmmap", "leaks", "malloc_history")
                                    if allocation_tracking
                                    else ("sample",)
                                ),
                            )
                            if owned.process.stdin is None:
                                raise RuntimeError("audited child lost its supervisor release pipe")
                            owned.process.stdin.write(b"\n")
                            owned.process.stdin.flush()
                            profiled = True
                        time.sleep(min(0.05, budget.remaining()))
                    status = budget.processes.wait(
                        owned,
                        timeout=min(remaining, budget.require_remaining(f"{phase}:wait")),
                    )
                record["exitStatus"] = status
                if round_output.exists():
                    _append_file(round_output, combined)
                if status != 0:
                    raise RuntimeError(
                        f"audit child {phase} failed: {stderr_path.read_text(errors='replace')[-4000:]}"
                    )
    finally:
        budget.terminate_registered()
        publish_json_exclusive(
            process_manifest,
            {
                "schemaVersion": 1,
                "deadlineMonotonic": budget.deadline,
                "incompletePhases": budget.incomplete_phases,
                "rounds": history,
            },
        )
