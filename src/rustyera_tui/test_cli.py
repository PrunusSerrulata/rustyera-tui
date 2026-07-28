"""Command-line entry point for deterministic and agent-driven TUI testing."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .abi import discover_library
from .testing import (
    ReferenceProcess,
    RustTestSession,
    Scenario,
    TestDriverError,
    TraceWriter,
    compare_observations,
    default_trace_path,
    goal_status,
    isolated_project_copy,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rustyera-test")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "serve"):
        command = subparsers.add_parser(name)
        command.add_argument("--scenario", type=Path, required=True)
        command.add_argument("--project", type=Path)
        command.add_argument("--runtime-library", type=Path)
        command.add_argument("--trace", type=Path)
        command.add_argument("--reference-command")
        command.add_argument("--reference-path-command")
        command.add_argument("--metrics-threshold-ms", type=float)
    return parser


def _reference(args: argparse.Namespace, scenario: Scenario) -> ReferenceProcess | None:
    enabled = scenario.comparison.get("reference", False)
    if scenario.start.type == "vm_snapshot":
        enabled = False
    if not enabled:
        return None
    configured_command = scenario.comparison.get("reference_command")
    command = (
        shlex.split(args.reference_command)
        if args.reference_command
        else shlex.split(configured_command)
        if isinstance(configured_command, str)
        else configured_command
    )
    if not command:
        raise TestDriverError("reference comparison requires --reference-command")
    configured_path_command = scenario.comparison.get("reference_path_command")
    path_command = (
        shlex.split(args.reference_path_command)
        if args.reference_path_command
        else shlex.split(configured_path_command)
        if isinstance(configured_path_command, str)
        else configured_path_command
    )
    return ReferenceProcess(
        list(command),
        list(path_command) if path_command else None,
        float(scenario.comparison.get("timeout_seconds", 30)),
    )


def _step_value(item: dict[str, Any], observation: dict[str, Any]) -> str | None:
    condition = item.get("when")
    if condition:
        output = "\n".join(observation.get("output", []))
        if "output_contains" in condition and condition["output_contains"] not in output:
            return None
    return str(item.get("value", ""))


def _decorate(
    rust: dict[str, Any],
    reference_observation: dict[str, Any] | None,
    scenario: Scenario,
    rust_session: RustTestSession,
    deadline: float,
) -> dict[str, Any]:
    if scenario.watches:
        rust["watches"] = rust_session.inspect(scenario.watches, deadline)
    else:
        rust["watches"] = {}
    result: dict[str, Any] = {"rust": rust}
    if reference_observation is not None:
        result["reference"] = reference_observation
        result["comparison"] = compare_observations(
            rust, reference_observation, scenario.comparison
        )
    result["goal"] = goal_status(rust, scenario.goal)
    return result


def execute(args: argparse.Namespace) -> int:
    scenario = Scenario.load(args.scenario, args.project)
    library = discover_library(args.runtime_library, scenario.project)
    trace_path = (args.trace or default_trace_path(scenario)).expanduser().resolve()
    trace = TraceWriter(trace_path, sys.stdout)
    rust: RustTestSession | None = None
    reference: ReferenceProcess | None = None
    isolated: tempfile.TemporaryDirectory[str] | None = None
    deadline = time.monotonic() + scenario.limits["timeout_seconds"]
    step = 0
    checkpoint_done = False
    try:
        reference = _reference(args, scenario)
        isolated = tempfile.TemporaryDirectory(prefix="isolated-projects-", dir=trace_path.parent)
        isolated_root = Path(isolated.name)
        rust_project = isolated_project_copy(scenario.project, isolated_root, "rust")
        reference_project = scenario.project
        if reference is not None:
            reference_project = isolated_project_copy(scenario.project, isolated_root, "reference")
        rust = RustTestSession(
            scenario,
            library,
            project_override=rust_project,
            metrics_threshold_ms=args.metrics_threshold_ms,
        )
        rust_observation = rust.wait_observation(deadline)
        reference_observation = reference.start(scenario, reference_project) if reference else None
        decorated = _decorate(rust_observation, reference_observation, scenario, rust, deadline)
        trace.emit(
            {
                "type": "observation",
                "step": step,
                "scenario": str(scenario.path),
                "seed": scenario.seed,
                **decorated,
            }
        )
        input_index = 0
        while step < scenario.limits["max_steps"]:
            if decorated.get("comparison", {}).get("equal") is False:
                trace.emit({"type": "result", "status": "difference", "trace": str(trace_path)})
                return 1
            if scenario.checkpoint and not checkpoint_done:
                wait = decorated["rust"]["wait"]
                if wait["stability"] == 0 and wait["deadline_ns"] is None:
                    configured = scenario.checkpoint.get("path")
                    if configured:
                        candidate = Path(configured).expanduser()
                        target = (
                            candidate
                            if candidate.is_absolute()
                            else scenario.path.parent / candidate
                        ).resolve()
                    else:
                        target = trace_path.parent / "checkpoint.snapshot"
                    rust.export_snapshot(target)
                    if rust.wait_snapshot(target, deadline):
                        checkpoint_done = True
                        trace.emit(
                            {
                                "type": "checkpoint",
                                "path": str(target),
                                "bytes": target.stat().st_size,
                            }
                        )
            if decorated["goal"]["satisfied"] and (not scenario.checkpoint or checkpoint_done):
                trace.emit({"type": "result", "status": "passed", "trace": str(trace_path)})
                return 0
            wait_kind = decorated["rust"]["wait"]["kind"]
            value: str | None = "" if wait_kind == 0 else None
            source = "automatic_enter" if value is not None else "fixed"
            while value is None and input_index < len(scenario.inputs):
                item = scenario.inputs[input_index]
                input_index += 1
                value = _step_value(item, decorated["rust"])
            if value is None and args.command == "serve":
                request_line = sys.stdin.readline()
                if not request_line:
                    raise TestDriverError("agent input stream closed")
                request = json.loads(request_line)
                operation = request.get("op")
                if operation == "stop":
                    trace.emit({"type": "result", "status": "stopped", "trace": str(trace_path)})
                    return 0
                if operation == "inspect":
                    watched = tuple(str(item) for item in request.get("watches", []))
                    trace.emit({"type": "inspection", "values": rust.inspect(watched, deadline)})
                    continue
                if operation == "export_snapshot":
                    target = Path(request["path"]).expanduser().resolve()
                    rust.export_snapshot(target)
                    trace.emit({"type": "checkpoint_requested", "path": str(target)})
                    continue
                if operation != "step":
                    raise TestDriverError(f"unknown agent operation {operation!r}")
                value = str(request.get("input", ""))
                source = "agent"
            elif value is None:
                pending_statuses = scenario.goal.get("status_contains", [])
                if pending_statuses:
                    for expected in pending_statuses:
                        rust.wait_for_status(str(expected), deadline)
                    decorated["rust"]["statuses"] = list(rust.statuses[-20:])
                    decorated["goal"] = goal_status(decorated["rust"], scenario.goal)
                if scenario.mode != "fixed":
                    status = "input_exhausted"
                elif scenario.checkpoint and not checkpoint_done:
                    status = "checkpoint_not_created"
                elif not scenario.goal or decorated["goal"]["satisfied"]:
                    status = "passed"
                else:
                    status = "goal_not_met"
                trace.emit({"type": "result", "status": status, "trace": str(trace_path)})
                return 0 if status == "passed" else 2 if status == "input_exhausted" else 1
            trace.emit({"type": "input", "step": step + 1, "source": source, "value": value})
            rust.submit(value)
            reference_observation = reference.step(value, scenario.watches) if reference else None
            step += 1
            rust_observation = rust.wait_observation(deadline)
            decorated = _decorate(rust_observation, reference_observation, scenario, rust, deadline)
            trace.emit({"type": "observation", "step": step, **decorated})
        trace.emit({"type": "result", "status": "budget_exhausted", "trace": str(trace_path)})
        return 2
    finally:
        if rust:
            rust.close()
        if reference:
            reference.close()
        if isolated:
            isolated.cleanup()
        trace.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return execute(build_parser().parse_args(argv))
    except (OSError, ValueError, TestDriverError, json.JSONDecodeError) as error:
        print(json.dumps({"type": "error", "message": str(error)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
