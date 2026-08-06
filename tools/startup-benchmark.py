#!/usr/bin/env python3
"""Measure TUI startup from process spawn using an isolated project fixture."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pty
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TELEMETRY_FD_ENV = "RUSTYERA_STARTUP_TELEMETRY_FD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("cold", "warm", "project_file"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--project-file", type=Path)
    parser.add_argument("--runtime-library", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


def isolated_project(source: Path, destination: Path) -> Path:
    target = destination / source.name
    shutil.copytree(
        source.resolve(strict=True),
        target,
        ignore=shutil.ignore_patterns(".rustyera"),
        symlinks=False,
    )
    return target


def terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def cache_proof_matches(scenario: str, cache_hit: object) -> bool:
    """Validate cache evidence without conflating package loading with cache exactness."""

    if scenario == "project_file":
        return isinstance(cache_hit, bool)
    return cache_hit is (scenario == "warm")


def launch(
    target: Path,
    *,
    project_file: bool,
    runtime_library: Path,
    timeout: float,
    wait_for_cache: bool = False,
) -> dict[str, Any]:
    runtime_path = runtime_library.resolve(strict=True)
    telemetry_read, telemetry_write = os.pipe()
    os.set_blocking(telemetry_write, False)
    try:
        master, slave = pty.openpty()
    except BaseException:
        os.close(telemetry_read)
        os.close(telemetry_write)
        raise
    env = os.environ.copy()
    env[TELEMETRY_FD_ENV] = str(telemetry_write)
    command = [sys.executable, "-m", "rustyera_tui"]
    if project_file:
        command.extend(("--project-file", str(target)))
    else:
        command.append(str(target))
    command.extend(("--runtime-library", str(runtime_path)))
    started_ns = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            pass_fds=(telemetry_write,),
            start_new_session=True,
        )
    except BaseException:
        os.close(telemetry_read)
        os.close(telemetry_write)
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    os.close(telemetry_write)
    selector: selectors.BaseSelector | None = None
    buffer = bytearray()
    events: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    try:
        selector = selectors.DefaultSelector()
        selector.register(telemetry_read, selectors.EVENT_READ, "telemetry")
        selector.register(master, selectors.EVENT_READ, "terminal")
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"TUI exited before startup completed (status {process.returncode})"
                )
            for key, _mask in selector.select(timeout=0.25):
                try:
                    chunk = os.read(key.fd, 64 * 1024)
                except OSError:
                    continue
                if key.data == "terminal" or not chunk:
                    continue
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    event = json.loads(raw)
                    event["external_elapsed_ms"] = (time.perf_counter_ns() - started_ns) / 1e6
                    events[event["event"]] = event
                    if event["event"] == "failed":
                        raise RuntimeError(f"TUI startup failed: {event}")
            target_event = "cache_persisted" if wait_for_cache else "first_game_phase"
            if target_event in events:
                break
        else:
            raise TimeoutError(f"TUI startup timed out after {timeout:.1f}s; events={events}")
    finally:
        with contextlib.suppress(BaseException):
            terminate(process)
        with contextlib.suppress(BaseException):
            if selector is not None:
                selector.close()
        with contextlib.suppress(OSError):
            os.close(telemetry_read)
        with contextlib.suppress(OSError):
            os.close(master)

    validation = events.get("validation_complete")
    first_phase = events.get("first_game_phase")
    result = {
        "attempt_id": validation.get("attempt_id") if validation else None,
        "client": "tui",
        "scenario": validation.get("scenario") if validation else None,
        "cache_hit": validation.get("cache_hit") if validation else None,
        "durations": {
            "enumerate_ms": None,
            "stat_and_index_read_ms": None,
            "index_write_ms": None,
            "source_read_decode_hash_ms": None,
            "cache_read_ms": None,
            "submission_transfer_ms": None,
            "cache_decode_ms": None,
            "parse_ms": None,
            "analyze_ms": None,
            "compile_ms": None,
            "validate_ms": None,
        },
        "milestones": {
            "validation_complete_ms": validation.get("external_elapsed_ms") if validation else None,
            "start_submitted_ms": events.get("start_submitted", {}).get("external_elapsed_ms"),
            "first_game_phase_ms": first_phase.get("external_elapsed_ms") if first_phase else None,
        },
    }
    return result


def main() -> None:
    args = build_parser().parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be at least 1")
    if args.scenario == "project_file" and args.project_file is None:
        raise SystemExit("--project-file is required for project_file")
    if args.scenario != "project_file" and args.project is None:
        raise SystemExit("--project is required for cold and warm")

    samples = []
    for sample_index in range(args.samples):
        with tempfile.TemporaryDirectory(prefix="rustyera-startup-") as temporary:
            temporary_path = Path(temporary)
            if args.scenario == "project_file":
                source = args.project_file.resolve(strict=True)
                target = temporary_path / source.name
                shutil.copy2(source, target)
                result = launch(
                    target,
                    project_file=True,
                    runtime_library=args.runtime_library,
                    timeout=args.timeout,
                )
            else:
                target = isolated_project(args.project, temporary_path)
                if args.scenario == "warm":
                    launch(
                        target,
                        project_file=False,
                        runtime_library=args.runtime_library,
                        timeout=args.timeout,
                        wait_for_cache=True,
                    )
                result = launch(
                    target,
                    project_file=False,
                    runtime_library=args.runtime_library,
                    timeout=args.timeout,
                )
            if not cache_proof_matches(args.scenario, result["cache_hit"]):
                raise RuntimeError(
                    f"scenario {args.scenario} cache proof mismatch: {result['cache_hit']}"
                )
            if result["scenario"] != args.scenario:
                raise RuntimeError(
                    f"scenario proof mismatch: expected {args.scenario}, got {result['scenario']}"
                )
            result["sample"] = sample_index + 1
            samples.append(result)
            print(json.dumps(result, separators=(",", ":")), flush=True)

    fields = ("validation_complete_ms", "first_game_phase_ms")
    summary = {
        "scenario": args.scenario,
        "samples": len(samples),
        "median_ms": {
            field: sorted(sample["milestones"][field] for sample in samples)[len(samples) // 2]
            for field in fields
        },
    }
    print(json.dumps({"summary": summary}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
