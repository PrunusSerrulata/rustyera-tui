"""Deterministic C-ABI test driver used by the repository testing CLI."""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .presentation import PresentationModel
from .runtime import FrontendEvent, PresentationBatch, RuntimeWorker
from .wire import unwrap_variant

SCENARIO_VERSION = 1
REFERENCE_SCHEMA_VERSION = 2
DEFAULT_LIMITS = {"max_steps": 100, "timeout_seconds": 300}
STREAM_OUTPUT_LINES = 30
TERMINAL_EVENTS = {"error", "runtime_error", "runtime_fault", "worker_stopped"}
INDEXED_WATCH = re.compile(r"^([^:@]+):(-?\d+(?:,-?\d+)*)$")


class TestDriverError(RuntimeError):
    """A scenario, runtime, or reference process could not be driven safely."""

    __test__ = False


@dataclass(frozen=True, slots=True)
class StartSpec:
    type: str = "new_game"
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    path: Path
    project: Path
    mode: str
    start: StartSpec
    seed: int | None
    inputs: tuple[dict[str, Any], ...]
    watches: tuple[str, ...]
    goal: dict[str, Any]
    limits: dict[str, int]
    comparison: dict[str, Any]
    checkpoint: dict[str, Any]

    @classmethod
    def load(cls, path: Path, project_override: Path | None = None) -> Scenario:
        resolved = path.expanduser().resolve(strict=True)
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        if raw.get("schema_version") != SCENARIO_VERSION:
            raise TestDriverError(f"unsupported scenario schema {raw.get('schema_version')!r}")
        mode = raw.get("mode", "fixed")
        if mode not in {"fixed", "autonomous"}:
            raise TestDriverError("scenario mode must be fixed or autonomous")
        project_value = project_override or Path(raw.get("project", "."))
        project = project_value if project_value.is_absolute() else resolved.parent / project_value
        project = project.expanduser().resolve(strict=True)
        start_raw = raw.get("start", {"type": "new_game"})
        start_type = start_raw.get("type", "new_game")
        if start_type not in {"new_game", "traditional_save", "vm_snapshot"}:
            raise TestDriverError(f"unknown start type {start_type!r}")
        start_path = start_raw.get("path")
        if start_type != "new_game" and not start_path:
            raise TestDriverError(f"{start_type} start requires path")
        state_path = None
        if start_path:
            candidate = Path(start_path)
            state_path = candidate if candidate.is_absolute() else resolved.parent / candidate
            state_path = state_path.expanduser().resolve(strict=True)
        configured_seed = raw.get("seed")
        seed = (
            secrets.randbelow(0x8000_0000)
            if configured_seed is None and start_type == "new_game"
            else int(configured_seed)
            if configured_seed is not None and start_type == "new_game"
            else None
        )
        if seed is not None and not 0 <= seed <= 0x7FFF_FFFF:
            raise TestDriverError("seed must be a non-negative 32-bit integer")
        inputs = tuple(
            {"value": item} if isinstance(item, (str, int)) else dict(item)
            for item in raw.get("inputs", [])
        )
        limits = {**DEFAULT_LIMITS, **raw.get("limits", {})}
        if limits["max_steps"] <= 0 or limits["timeout_seconds"] <= 0:
            raise TestDriverError("scenario limits must be positive")
        return cls(
            resolved,
            project,
            mode,
            StartSpec(start_type, state_path),
            seed,
            inputs,
            tuple(str(item) for item in raw.get("watches", [])),
            dict(raw.get("goal", {})),
            limits,
            dict(raw.get("comparison", {})),
            dict(raw.get("checkpoint", {})),
        )


@dataclass(frozen=True, slots=True)
class OutputDelta:
    reset: bool
    removed: int
    added: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"reset": self.reset, "removed": self.removed, "added": list(self.added)}


def normalized_lines(lines: list[str], ignore: list[str] | None = None) -> list[str]:
    patterns = [re.compile(pattern) for pattern in (ignore or [])]
    result: list[str] = []
    for line in lines:
        value = line.replace("\r", "").rstrip()
        if any(pattern.search(value) for pattern in patterns):
            continue
        result.append(value)
    return result


def output_delta(previous: list[str], current: list[str]) -> OutputDelta:
    common = 0
    for left, right in zip(previous, current, strict=False):
        if left != right:
            break
        common += 1
    removed = len(previous) - common
    return OutputDelta(common == 0 and bool(previous), removed, tuple(current[common:]))


def plain_output(model: PresentationModel) -> list[str]:
    return ["".join(segment.text for segment in line.segments) for line in model.lines]


def apply_presentation_event(
    model: PresentationModel, event: FrontendEvent
) -> dict[int, Any] | None:
    if event.kind != "presentation_batch":
        return None
    batch = event.value
    if not isinstance(batch, PresentationBatch):
        raise TestDriverError("worker returned an invalid presentation batch")
    if batch.snapshot is not None:
        model.apply_snapshot(batch.snapshot)
    if batch.delta is not None:
        model.apply_delta(batch.delta)
    return batch.active_wait


def _debug_value(value: Any) -> Any:
    if value is None:
        return None
    tag, fields = unwrap_variant(value)
    if tag in {0, 1, 2} and fields:
        return fields[0]
    return {"tag": tag, "fields": fields}


@dataclass(slots=True)
class RustTestSession:
    scenario: Scenario
    runtime_library: Path | None
    project_override: Path | None = None
    metrics_threshold_ms: float | None = None
    model: PresentationModel = field(default_factory=PresentationModel)
    previous_output: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    _last_wait: tuple[int, Any] | None = None
    worker: RuntimeWorker = field(init=False)

    def __post_init__(self) -> None:
        state = None
        if self.scenario.start.path is not None:
            state = (self.scenario.start.path, self.scenario.start.type)
        seed = self.scenario.seed if self.scenario.start.type == "new_game" else None
        project = self.project_override or self.scenario.project
        self.worker = RuntimeWorker(
            self.runtime_library,
            project,
            new_game_seed=seed,
            metrics_threshold_ms=self.metrics_threshold_ms,
            initial_state=state,
        )
        self.worker.start()

    def close(self) -> None:
        self.worker.stop()
        self.worker.join(timeout=5)

    def submit(self, value: str) -> None:
        self.worker.send("submit_text", value)

    def export_snapshot(self, path: Path) -> None:
        self.worker.send("export_snapshot", (path, "normal"))

    def wait_snapshot(self, path: Path, deadline: float) -> bool:
        while time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.25)
            except queue.Empty:
                continue
            apply_presentation_event(self.model, event)
            if event.kind == "snapshot_export_finished":
                return bool(event.value) and path.is_file()
            if event.kind == "runtime_error" and "不能生成快照" in str(event.value):
                return False
            if event.kind in TERMINAL_EVENTS:
                raise TestDriverError(f"{event.kind}: {event.value}")
        raise TestDriverError("timed out waiting for snapshot export")

    def wait_for_status(self, text: str, deadline: float) -> None:
        while text not in self.statuses and time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.25)
            except queue.Empty:
                continue
            apply_presentation_event(self.model, event)
            if event.kind == "status":
                self.statuses.append(str(event.value))
            elif event.kind in TERMINAL_EVENTS:
                raise TestDriverError(f"{event.kind}: {event.value}")
        if text not in self.statuses:
            raise TestDriverError(f"timed out waiting for status {text!r}")

    def wait_observation(self, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.25)
            except queue.Empty:
                if not self.worker.is_alive():
                    raise TestDriverError("runtime worker stopped")
                continue
            if event.kind == "status":
                self.statuses.append(str(event.value))
            elif event.kind == "runtime_metrics":
                self.metrics.append(dict(event.value))
            elif event.kind in TERMINAL_EVENTS:
                raise TestDriverError(f"{event.kind}: {event.value}")
            wait = apply_presentation_event(self.model, event)
            if wait is None:
                continue
            identity = (wait[0], wait.get(11))
            if identity == self._last_wait:
                continue
            self._last_wait = identity
            current = plain_output(self.model)
            delta = output_delta(self.previous_output, current)
            self.previous_output = current
            return {
                "termination": "waitingInput",
                "phase": self.worker.client.phase if self.worker.client else None,
                "wait": {
                    "id": wait[0],
                    "kind": wait[1],
                    "stability": wait[2],
                    "system_input": wait[5],
                    "deadline_ns": wait.get(8),
                },
                "output": current,
                "output_delta": delta.as_dict(),
                "output_tail": current[-30:],
                "metrics": list(self.metrics),
                "statuses": list(self.statuses[-20:]),
            }
        raise TestDriverError("timed out waiting for a stable runtime input")

    def inspect(self, watches: tuple[str, ...], deadline: float) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for expression in watches:
            indexed = INDEXED_WATCH.fullmatch(expression.strip())
            if indexed:
                self.worker.send("debug_action", ("variables", None))
            else:
                self.worker.send("debug_action", ("console_evaluate", expression))
            while time.monotonic() < deadline:
                try:
                    event = self.worker.events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if event.kind in TERMINAL_EVENTS:
                    raise TestDriverError(f"{event.kind}: {event.value}")
                if event.kind != "debug_response":
                    continue
                pending, response_tag, fields = event.value
                if pending == "console" and response_tag == 8 and fields and not indexed:
                    outcome = fields[0]
                    values[expression] = _debug_value(outcome.get(1))
                    break
                if pending == "variables" and response_tag == 1 and fields and indexed:
                    descriptors = fields[0].get(1, [])
                    descriptor = next(
                        (
                            item
                            for item in descriptors
                            if str(item.get(1, "")).casefold() == indexed.group(1).casefold()
                        ),
                        None,
                    )
                    if descriptor is None:
                        raise TestDriverError(f"debug variable is not visible: {indexed.group(1)}")
                    indices = tuple(int(item) for item in indexed.group(2).split(","))
                    self.worker.send("debug_action", ("read_variable", (descriptor, indices)))
                    continue
                if pending == "variable_value" and response_tag == 2 and fields and indexed:
                    values[expression] = _debug_value(fields[0].get(1))
                    break
            else:
                raise TestDriverError(f"timed out inspecting {expression}")
            self.worker.send("debug_surface_closed", "variables" if indexed else "console")
            while time.monotonic() < deadline:
                try:
                    event = self.worker.events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if event.kind in TERMINAL_EVENTS:
                    raise TestDriverError(f"{event.kind}: {event.value}")
                if event.kind == "debug_response" and event.value[0] == "transient_continue":
                    break
            else:
                raise TestDriverError("timed out resuming after state inspection")
        return values


class ReferenceProcess:
    def __init__(
        self,
        command: list[str],
        path_converter: list[str] | None = None,
        timeout_seconds: float = 30,
    ):
        self.path_converter = path_converter
        self.timeout_seconds = timeout_seconds
        self.process = subprocess.Popen(  # noqa: S603 - explicit scenario-owned command
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.next_id = 1
        self.schema_version: int | None = None
        self.reference_commit: str | None = None
        self.previous_output: list[str] = []
        self.responses: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_responses, daemon=True)
        self.reader.start()

    def _read_responses(self) -> None:
        if not self.process.stdout:
            self.responses.put(None)
            return
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()

    def convert_path(self, path: Path) -> str:
        if not self.path_converter:
            return str(path)
        completed = subprocess.run(  # noqa: S603 - explicit scenario-owned command
            [*self.path_converter, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        if not self.process.stdin:
            raise TestDriverError("reference process pipes are unavailable")
        request = {"id": self.next_id, "op": operation, **fields}
        self.next_id += 1
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            line = self.responses.get(timeout=self.timeout_seconds)
        except queue.Empty as error:
            raise TestDriverError("reference process timed out") from error
        if not line:
            detail = self.process.stderr.read() if self.process.stderr else ""
            raise TestDriverError(f"reference process stopped without a response: {detail}")
        response = json.loads(line)
        if not response.get("ok"):
            raise TestDriverError(f"reference request failed: {response.get('error')}")
        schema_version = response.get("schemaVersion")
        if schema_version != REFERENCE_SCHEMA_VERSION:
            raise TestDriverError(
                f"reference schema {schema_version!r} is not {REFERENCE_SCHEMA_VERSION}"
            )
        if response.get("id") != request["id"]:
            raise TestDriverError("reference response id does not match its request")
        self.schema_version = schema_version
        self.reference_commit = response.get("referenceCommit")
        return response["result"]

    def start(self, scenario: Scenario, project_override: Path | None = None) -> dict[str, Any]:
        capabilities = self.request("capabilities")
        operations = set(capabilities.get("operations", []))
        required = {"load", "run"}
        if scenario.start.type == "traditional_save":
            required.add("loadSave")
        missing = sorted(required - operations)
        if missing:
            raise TestDriverError(f"reference CLI is missing operations: {', '.join(missing)}")
        result = self.request(
            "load",
            gameDir=self.convert_path(project_override or scenario.project),
            seed=scenario.seed,
            watch=list(scenario.watches),
        )
        if scenario.start.type == "traditional_save" and scenario.start.path is not None:
            result = self.request(
                "loadSave",
                savePath=self.convert_path(scenario.start.path),
                watch=list(scenario.watches),
            )
        return self.observe(result)

    def step(self, value: str, watches: tuple[str, ...]) -> dict[str, Any]:
        return self.observe(self.request("run", inputs=[value], watch=list(watches)))

    def observe(self, result: dict[str, Any]) -> dict[str, Any]:
        current = [str(item) for item in result.get("output", [])]
        delta = output_delta(self.previous_output, current)
        self.previous_output = current
        request = result.get("inputRequest") or {}
        return {
            "termination": result.get("termination"),
            "wait": {
                "kind": request.get("InputType"),
                "system_input": request.get("IsSystemInput", False),
            },
            "output": current,
            "output_delta": delta.as_dict(),
            "output_tail": current[-30:],
            "watches": result.get("watches", {}),
            "random_seed": result.get("randomSeed"),
            "random_algorithm": result.get("randomAlgorithm"),
            "schema_version": self.schema_version,
            "reference_commit": self.reference_commit,
        }


class TraceWriter:
    def __init__(self, path: Path, stream: TextIO):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")
        self.stream = stream

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.file.write(line + "\n")
        self.file.flush()
        stream_event = dict(event)
        if event.get("type") == "observation":
            for implementation in ("rust", "reference"):
                observation = event.get(implementation)
                if isinstance(observation, dict):
                    streamed_observation = {
                        key: value for key, value in observation.items() if key != "output"
                    }
                    delta = observation.get("output_delta")
                    if isinstance(delta, dict) and isinstance(delta.get("added"), list):
                        added = delta["added"]
                        if len(added) > STREAM_OUTPUT_LINES:
                            streamed_observation["output_delta"] = {
                                **delta,
                                "added": added[-STREAM_OUTPUT_LINES:],
                                "added_omitted": len(added) - STREAM_OUTPUT_LINES,
                            }
                    stream_event[implementation] = streamed_observation
        self.stream.write(
            json.dumps(stream_event, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.stream.flush()

    def close(self) -> None:
        self.file.close()


def compare_observations(
    rust: dict[str, Any], reference: dict[str, Any], comparison: dict[str, Any]
) -> dict[str, Any]:
    default_wait_kind_map = {
        "0": "EnterKey",
        "1": "AnyKey",
        "2": "IntValue",
        "3": "StrValue",
        "4": "Void",
        "5": "AnyValue",
        "6": "IntButton",
        "7": "StrButton",
        "8": "PrimitiveMouseKey",
    }
    ignore = list(comparison.get("ignore_output", []))
    rust_added = normalized_lines(rust["output_delta"]["added"], ignore)
    reference_added = normalized_lines(reference["output_delta"]["added"], ignore)
    differences: dict[str, Any] = {}
    if rust_added != reference_added:
        differences["output_delta"] = {"rust": rust_added, "reference": reference_added}
    wait_kind_map = {**default_wait_kind_map, **comparison.get("wait_kind_map", {})}
    expected_wait = wait_kind_map.get(str(rust["wait"]["kind"]))
    if expected_wait is not None and expected_wait != reference["wait"]["kind"]:
        differences["wait_kind"] = {
            "rust": rust["wait"]["kind"],
            "reference": reference["wait"]["kind"],
        }
    rust_watches = rust.get("watches", {})
    reference_watches = reference.get("watches", {})
    if rust_watches != reference_watches:
        differences["watches"] = {"rust": rust_watches, "reference": reference_watches}
    return {"equal": not differences, "differences": differences}


def goal_status(observation: dict[str, Any], goal: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    joined = "\n".join(observation.get("output", []))
    for text in goal.get("output_contains", []):
        checks[f"output_contains:{text}"] = str(text) in joined
    if "wait_kind" in goal:
        checks["wait_kind"] = observation.get("wait", {}).get("kind") == goal["wait_kind"]
    if "termination" in goal:
        checks["termination"] = observation.get("termination") == goal["termination"]
    for name, expected in goal.get("watch_equals", {}).items():
        checks[f"watch_equals:{name}"] = observation.get("watches", {}).get(name) == expected
    if "line_count_lte" in goal:
        checks["line_count_lte"] = len(observation.get("output", [])) <= goal["line_count_lte"]
    for text in goal.get("status_contains", []):
        checks[f"status_contains:{text}"] = any(
            str(text) in status for status in observation.get("statuses", [])
        )
    return {"satisfied": bool(checks) and all(checks.values()), "checks": checks}


def default_trace_path(scenario: Scenario) -> Path:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = Path(os.environ.get("ERA_TEST_OUTPUT_DIR", ".rustyera/test-runs"))
    return root / f"{scenario.path.stem}-{run_id}" / "trace.ndjson"


def isolated_project_copy(source: Path, root: Path, name: str) -> Path:
    """Copy a game for a mutating frontend while excluding RustyEra's generated cache."""

    destination = root / name
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".rustyera"))
    return destination
