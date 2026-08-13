#!/usr/bin/env python3
"""Measure TUI startup from process spawn using an isolated project fixture."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import pty
except ImportError:

    class _UnavailablePty:
        @staticmethod
        def openpty() -> tuple[int, int]:
            raise RuntimeError("startup benchmark requires a POSIX pseudo-terminal")

    pty = _UnavailablePty()

TELEMETRY_FD_ENV = "RUSTYERA_STARTUP_TELEMETRY_FD"
MILESTONE_FIELDS = (
    "validation_complete_ms",
    "start_submitted_ms",
    "first_game_phase_ms",
)
DIRECTORY_HOST_FIELDS = {
    "enumerate_ms",
    "index_read_ms",
    "index_write_ms",
    "stat_ms",
    "source_read_decode_hash_ms",
    "cache_read_ms",
    "submission_transfer_ms",
}
COLD_CORE_FIELDS = {
    "normalize_ms",
    "csv_ms",
    "parse_ms",
    "analyze_ms",
    "compile_ms",
    "compile_finalize_ms",
    "validate_ms",
    "prepare_ms",
}
CACHE_CORE_FIELDS = {"cache_parse_ms", "cache_decode_ms", "cache_validate_ms", "prepare_ms"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=("cold-no-index", "cold-indexed", "warm", "project_file"),
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--project-file", type=Path)
    parser.add_argument("--runtime-library", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
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


def source_index_proof_matches(scenario: str, proof: dict[str, Any]) -> bool:
    if scenario == "project_file":
        return True
    expected = scenario in {"cold-indexed", "warm"}
    reused = proof.get("source_files_reused")
    hashed = proof.get("source_files_hashed")
    counts_match = (
        isinstance(reused, int)
        and isinstance(hashed, int)
        and reused + hashed > 0
        and (reused > 0 if expected else (reused == 0 and hashed > 0))
    )
    return (
        proof.get("source_index_present") is expected
        and proof.get("index_existed_before_launch") is expected
        and counts_match
    )


def percentile(values: list[float], fraction: float) -> float:
    """Return a nearest-rank percentile for a small benchmark sample."""

    ordered = sorted(values)
    rank = max(1, int(len(ordered) * fraction + 0.999_999))
    return ordered[min(rank, len(ordered)) - 1]


def validate_sample(scenario: str, result: dict[str, Any]) -> None:
    """Reject incomplete telemetry rather than silently producing partial percentiles."""

    missing_milestones = [
        field
        for field in MILESTONE_FIELDS
        if not isinstance(result.get("milestones", {}).get(field), int | float)
    ]
    required_durations = {"cache_read_ms", "submission_transfer_ms"}
    if scenario != "project_file":
        required_durations.update(DIRECTORY_HOST_FIELDS)
    if result.get("cache_hit") is True:
        required_durations.update(CACHE_CORE_FIELDS)
    else:
        required_durations.update(COLD_CORE_FIELDS)
        required_durations.add("source_materialize_ms")
    missing_durations = [
        field
        for field in sorted(required_durations)
        if not isinstance(result.get("durations", {}).get(field), int | float)
    ]
    peak_rss = result.get("peak_rss_bytes")
    if not isinstance(peak_rss, int) or peak_rss <= 0:
        missing_durations.append("peak_rss_bytes")
    if missing_milestones or missing_durations:
        raise RuntimeError(
            "startup telemetry is incomplete: "
            f"milestones={missing_milestones}, durations={missing_durations}"
        )


def merge_phase_metrics(
    event: dict[str, Any], host_metrics: dict[str, Any], core_metrics: dict[str, Any]
) -> None:
    if event.get("event") == "host_metrics":
        metadata = {
            "event",
            "client",
            "runtime_monotonic_ns",
            "peak_rss_bytes",
            "attempt_id",
            "external_elapsed_ms",
        }
        host_metrics.update((key, value) for key, value in event.items() if key not in metadata)
    elif event.get("event") == "core_phase":
        phase = event.get("phase")
        duration = event.get("duration_ms")
        if isinstance(phase, str) and isinstance(duration, int | float):
            core_metrics[phase] = duration


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
    host_metrics: dict[str, Any] = {}
    core_metrics: dict[str, Any] = {}
    peak_rss_bytes = 0
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
                    peak_rss_bytes = max(peak_rss_bytes, int(event.get("peak_rss_bytes", 0)))
                    event["external_elapsed_ms"] = (time.perf_counter_ns() - started_ns) / 1e6
                    merge_phase_metrics(event, host_metrics, core_metrics)
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
    host = host_metrics
    core = core_metrics
    result = {
        "attempt_id": validation.get("attempt_id") if validation else None,
        "client": "tui",
        "scenario": validation.get("scenario") if validation else None,
        "cache_hit": validation.get("cache_hit") if validation else None,
        "durations": {
            "enumerate_ms": host.get("enumerate_ms"),
            "index_read_ms": host.get("index_read_ms"),
            "index_write_ms": host.get("index_write_ms"),
            "stat_ms": host.get("stat_ms"),
            "source_read_decode_hash_ms": host.get("source_read_decode_hash_ms"),
            "source_materialize_ms": host.get("source_materialize_ms"),
            "cache_read_ms": host.get("cache_read_ms"),
            "submission_transfer_ms": host.get("submission_transfer_ms"),
            "cache_parse_ms": core.get("cache_parse_ms"),
            "cache_decode_ms": core.get("cache_decode_ms"),
            "cache_validate_ms": core.get("cache_validate_ms"),
            "normalize_ms": core.get("normalize_ms"),
            "csv_ms": core.get("csv_ms"),
            "parse_ms": core.get("parse_ms"),
            "analyze_ms": core.get("analyze_ms"),
            "compile_ms": core.get("compile_ms"),
            "compile_finalize_ms": core.get("compile_finalize_ms"),
            "validate_ms": core.get("validate_ms"),
            "prepare_ms": core.get("prepare_ms"),
        },
        "proof": {
            "source_index_present": host.get("source_index_present"),
            "source_files_reused": host.get("source_files_reused"),
            "source_files_hashed": host.get("source_files_hashed"),
        },
        "peak_rss_bytes": peak_rss_bytes,
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
        raise SystemExit("--project is required for directory scenarios")

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
                index_path = target / ".rustyera" / "cache" / "source-index-v1.json"
                if args.scenario == "cold-indexed":
                    from rustyera_tui.project import ProjectBundle

                    ProjectBundle.scan_quick(target)
                if args.scenario == "warm":
                    launch(
                        target,
                        project_file=False,
                        runtime_library=args.runtime_library,
                        timeout=args.timeout,
                        wait_for_cache=True,
                    )
                index_existed_before_launch = index_path.is_file()
                result = launch(
                    target,
                    project_file=False,
                    runtime_library=args.runtime_library,
                    timeout=args.timeout,
                )
                result["proof"]["index_existed_before_launch"] = index_existed_before_launch
            if not cache_proof_matches(args.scenario, result["cache_hit"]):
                raise RuntimeError(
                    f"scenario {args.scenario} cache proof mismatch: {result['cache_hit']}"
                )
            if not source_index_proof_matches(args.scenario, result["proof"]):
                raise RuntimeError(
                    f"scenario {args.scenario} index proof mismatch: {result['proof']}"
                )
            expected_runtime_scenario = (
                "cold" if args.scenario in {"cold-no-index", "cold-indexed"} else args.scenario
            )
            if result["scenario"] != expected_runtime_scenario:
                raise RuntimeError(
                    "scenario proof mismatch: "
                    f"expected {expected_runtime_scenario}, got {result['scenario']}"
                )
            validate_sample(args.scenario, result)
            result["sample"] = sample_index + 1
            samples.append(result)
            print(json.dumps(result, separators=(",", ":")), flush=True)

    def summarized(field: str, source: str) -> list[float]:
        values = [sample[source].get(field) for sample in samples]
        return [float(value) for value in values if isinstance(value, int | float)]

    duration_fields = tuple(samples[0]["durations"])
    summary = {
        "scenario": args.scenario,
        "samples": len(samples),
        "p50_ms": {
            **{
                field: percentile(summarized(field, "milestones"), 0.5)
                for field in MILESTONE_FIELDS
            },
            **{
                field: percentile(values, 0.5)
                for field in duration_fields
                if (values := summarized(field, "durations"))
            },
        },
        "p95_ms": {
            **{
                field: percentile(summarized(field, "milestones"), 0.95)
                for field in MILESTONE_FIELDS
            },
            **{
                field: percentile(values, 0.95)
                for field in duration_fields
                if (values := summarized(field, "durations"))
            },
        },
        "p50_peak_rss_bytes": percentile(
            [float(sample["peak_rss_bytes"]) for sample in samples], 0.5
        ),
        "p95_peak_rss_bytes": percentile(
            [float(sample["peak_rss_bytes"]) for sample in samples], 0.95
        ),
        "peak_rss_bytes": max(sample["peak_rss_bytes"] for sample in samples),
    }
    print(json.dumps({"summary": summary}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
