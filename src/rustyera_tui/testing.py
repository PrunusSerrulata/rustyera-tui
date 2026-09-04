"""Deterministic C-ABI test driver used by the repository testing CLI."""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import sys
import time
import traceback
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .presentation import PresentationModel
from .runtime import FrontendEvent, PresentationBatch, RuntimeWorker
from .storage import StorageBackend
from .testing_reference import ReferenceProcess as ReferenceProcess
from .testing_scenario import Scenario as _Scenario, StartSpec as StartSpec
from .testing_support import (
    OutputDelta as OutputDelta,
    TestDriverError as TestDriverError,
    normalized_lines as normalized_lines,
    output_delta as output_delta,
)
from .testing_trace import TraceWriter as TraceWriter
from .wire import unwrap_variant

TERMINAL_EVENTS = {"error", "runtime_error", "runtime_fault", "worker_stopped"}
INDEXED_WATCH = re.compile(r"^([^:@]+):(-?\d+(?:,-?\d+)*)$")


class Scenario(_Scenario):
    """Compatibility facade preserving the patchable random source."""

    @classmethod
    def random_seed(cls) -> int:
        return secrets.randbelow(0x8000_0000)


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
    logs: list[str] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    _last_wait: tuple[int, Any] | None = None
    project_root: Path = field(init=False)
    worker: RuntimeWorker = field(init=False)

    def __post_init__(self) -> None:
        state = None
        if self.scenario.start.path is not None:
            state = (self.scenario.start.path, self.scenario.start.type)
        seed = self.scenario.seed if self.scenario.start.type == "new_game" else None
        project = self.project_override or self.scenario.project
        self.project_root = project.resolve(strict=True)
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

    def skip_message(self) -> None:
        self.worker.send("skip_message_waits")

    def activate_last_button(self) -> dict[int, int]:
        for line in reversed(self.model.lines):
            for segment in reversed(line.segments):
                if segment.token is not None and self.model.segment_enabled(segment):
                    token = dict(segment.token)
                    self.worker.send("activate", token)
                    return token
        raise TestDriverError("the current presentation has no enabled button")

    def restart(self) -> None:
        self.model = PresentationModel()
        self.previous_output = []
        self._last_wait = None
        self.worker.send("restart")

    def edit_source(self, relative_path: str, expected: str, replacement: str) -> None:
        target = self._project_path(relative_path)
        if not target.is_file():
            raise TestDriverError("source edit path must name a project file")
        contents = target.read_text(encoding="utf-8")
        if contents.count(expected) != 1:
            raise TestDriverError("source edit expected text must occur exactly once")
        target.write_text(contents.replace(expected, replacement, 1), encoding="utf-8")

    def reload(self, scope: str, relative_path: str | None = None) -> None:
        if scope == "all":
            if relative_path is not None:
                raise TestDriverError("all reload does not accept a path")
            self.worker.send("reload_all")
            return
        if scope not in {"folder", "file"} or relative_path is None:
            raise TestDriverError("reload scope must be all, folder, or file")
        target = self._project_path(relative_path)
        if scope == "folder" and not target.is_dir():
            raise TestDriverError("folder reload path must name a project folder")
        if scope == "file" and not target.is_file():
            raise TestDriverError("file reload path must name a project file")
        self.worker.send(f"reload_{scope}", target)

    def _project_path(self, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise TestDriverError("source path must stay inside the isolated project")
        target = self.project_root.joinpath(*relative.parts).resolve(strict=True)
        if not target.is_relative_to(self.project_root):
            raise TestDriverError("source path must stay inside the isolated project")
        return target

    def export_snapshot(self, path: Path) -> None:
        self.worker.send("export_snapshot", (path, "normal"))

    def restore_snapshot(self, path: Path) -> None:
        self.worker.send("restore_snapshot", path)

    def _acknowledge_frontend_boundary(self, event: FrontendEvent) -> bool:
        if event.kind != "device_pump":
            return False
        # The CLI has no Textual event loop, so its next driver callback is the
        # corresponding real frontend pump boundary.
        self.worker.send("device_pump_ack", int(event.value))
        return True

    def wait_snapshot(self, path: Path, deadline: float) -> bool:
        while time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if self._acknowledge_frontend_boundary(event):
                continue
            apply_presentation_event(self.model, event)
            if event.kind == "snapshot_export_finished":
                return bool(event.value) and path.is_file()
            if event.kind == "runtime_error" and "不能生成快照" in str(event.value):
                return False
            if event.kind in TERMINAL_EVENTS:
                raise TestDriverError(f"{event.kind}: {event.value}")
        raise TestDriverError("timed out waiting for snapshot export")

    def wait_for_status(self, text: str, deadline: float) -> str:
        while text not in self.statuses and time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if self._acknowledge_frontend_boundary(event):
                continue
            apply_presentation_event(self.model, event)
            if event.kind == "status":
                self.statuses.append(str(event.value))
            elif event.kind == "log":
                self.logs.append(str(event.value))
            elif event.kind in TERMINAL_EVENTS:
                raise TestDriverError(f"{event.kind}: {event.value}")
        if text not in self.statuses:
            raise TestDriverError(f"timed out waiting for status {text!r}")
        return next(status for status in reversed(self.statuses) if text in status)

    def wait_observation(
        self,
        deadline: float,
        *,
        completion_status: str | None = None,
    ) -> dict[str, Any]:
        completed = False
        observed: list[str] = []
        while time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.25)
            except queue.Empty:
                if not self.worker.is_alive():
                    raise TestDriverError("runtime worker stopped")
                if completed and self.worker.client and self.worker.client.active_wait is not None:
                    return self._observation(self.worker.client.active_wait)
                continue
            observed.append(f"{event.kind}: {event.value}")
            if self._acknowledge_frontend_boundary(event):
                continue
            if event.kind == "status":
                self.statuses.append(str(event.value))
                completed = completed or (
                    completion_status is not None and completion_status in str(event.value)
                )
            elif event.kind == "log":
                self.logs.append(str(event.value))
            elif event.kind == "runtime_metrics":
                self.metrics.append(dict(event.value))
            elif event.kind in TERMINAL_EVENTS:
                client = self.worker.client
                state = (
                    None
                    if client is None
                    else {
                        "phase": client.phase,
                        "epoch": client.epoch,
                        "active_wait": client.active_wait,
                    }
                )
                raise TestDriverError(
                    f"{event.kind}: {event.value}; state={state}; "
                    f"statuses={self.statuses[-20:]}; logs={self.logs[-50:]}"
                )
            wait = apply_presentation_event(self.model, event)
            if wait is None:
                continue
            identity = (wait[0], wait.get(11))
            if identity == self._last_wait:
                continue
            return self._observation(wait)
        client = self.worker.client
        frame = sys._current_frames().get(self.worker.ident)
        state = (
            None
            if client is None
            else {
                "phase": client.phase,
                "epoch": client.epoch,
                "active_wait": client.active_wait,
                "reload_revision": (
                    client.reload_candidate.revision
                    if client.reload_candidate is not None
                    else None
                ),
                "queued_commands": self.worker.commands.qsize(),
                "worker_stack": traceback.format_stack(frame) if frame is not None else [],
            }
        )
        raise TestDriverError(
            "timed out waiting for a stable runtime input; "
            f"state={state}; observed={observed[-30:]}"
        )

    def _observation(self, wait: dict[int, Any]) -> dict[str, Any]:
        self._last_wait = (wait[0], wait.get(11))
        current = plain_output(self.model)
        delta = output_delta(self.previous_output, current)
        self.previous_output = current
        return {
            "termination": "waitingInput",
            "phase": self.worker.client.phase if self.worker.client else None,
            "epoch": self.worker.client.epoch if self.worker.client else None,
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
            "logs": list(self.logs[-50:]),
        }

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
                if self._acknowledge_frontend_boundary(event):
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
                if self._acknowledge_frontend_boundary(event):
                    continue
                if event.kind in TERMINAL_EVENTS:
                    raise TestDriverError(f"{event.kind}: {event.value}")
                if event.kind == "debug_response" and event.value[0] == "transient_continue":
                    break
            else:
                raise TestDriverError("timed out resuming after state inspection")
        return values


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


def install_test_compiled_cache(project: Path, source: Path | None) -> None:
    """Install an opaque cross-host cache in an isolated CLI project."""

    if source is None:
        return
    cache = source.expanduser().resolve(strict=True)
    destination = StorageBackend(project).compiled_cache_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache, destination)


def install_test_source_index(project: Path, source: Path | None) -> None:
    """Install a cross-frontend source index beside an isolated CLI project."""

    if source is None:
        return
    index = source.expanduser().resolve(strict=True)
    destination = project / ".rustyera" / "cache" / "source-index-v1.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index, destination)
    document = json.loads(index.read_text(encoding="utf-8"))
    for relative_path, entry in document.get("files", {}).items():
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise TestDriverError("source-index path must stay inside the isolated project")
        target = project.joinpath(*relative.parts).resolve(strict=True)
        if not target.is_relative_to(project.resolve()):
            raise TestDriverError("source-index path must stay inside the isolated project")
        signature = entry.get("signature")
        match = re.fullmatch(r"(\d+):(\d+)", signature if isinstance(signature, str) else "")
        if match is None or target.stat().st_size != int(match[1]):
            raise TestDriverError("source-index signature does not match the isolated project")
        current = target.stat()
        os.utime(target, ns=(current.st_atime_ns, int(match[2]) * 1_000_000 + 500_000))


def publish_test_handoff(
    session: RustTestSession,
    project: Path,
    source_project: Path,
    cache_input: Path | None,
    source_index_input: Path | None,
    cache_target: Path | None,
    source_index_target: Path | None,
    project_target: Path | None,
) -> None:
    """Atomically publish one successful RuntimeWorker cross-host handoff."""

    if cache_target is None and source_index_target is None and project_target is None:
        return
    client = session.worker.client
    if client is None or client.storage is None:
        raise TestDriverError("runtime did not initialize project storage for cache export")
    cache_source = client.storage.compiled_cache_path()
    source_index_source = project / ".rustyera" / "cache" / "source-index-v1.json"
    if cache_target is not None and not cache_source.is_file():
        raise TestDriverError("runtime did not persist a compiled project cache")
    if source_index_target is not None and not source_index_source.is_file():
        raise TestDriverError("frontend did not persist a project source index")
    source_root = source_project.resolve()
    isolated_root = project.resolve()
    cache = cache_target.resolve() if cache_target is not None else None
    source_index = source_index_target.resolve() if source_index_target is not None else None
    output = project_target.resolve() if project_target is not None else None
    if cache_input is not None and cache is not None and cache_input.resolve() == cache:
        raise TestDriverError("cache input and output must differ")
    if (
        source_index_input is not None
        and source_index is not None
        and source_index_input.resolve() == source_index
    ):
        raise TestDriverError("source-index input and output must differ")
    targets = [target for target in (cache, source_index, output) if target is not None]
    if len(set(targets)) != len(targets):
        raise TestDriverError("cross-host artifact outputs must differ")
    for target in targets:
        if target is not None and (
            target == source_root
            or target.is_relative_to(source_root)
            or source_root.is_relative_to(target)
            or target == isolated_root
            or target.is_relative_to(isolated_root)
            or isolated_root.is_relative_to(target)
        ):
            raise TestDriverError("cross-host artifact target overlaps project state")
    if output is not None and output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise TestDriverError("project output target must be absent or empty")
    if cache is not None and cache.exists():
        raise TestDriverError("cache output target must not exist")
    if source_index is not None and source_index.exists():
        raise TestDriverError("source-index output target must not exist")
    parents = {target.parent for target in targets}
    if len(parents) != 1:
        raise TestDriverError("cross-host cache and project outputs must share a parent directory")
    parent = next(iter(parents))
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".handoff-", dir=parent) as temporary_name:
        temporary = Path(temporary_name)
        if cache is not None:
            shutil.copy2(cache_source, temporary / "compiled-project.reracache")
        if source_index is not None:
            shutil.copy2(source_index_source, temporary / "source-index-v1.json")
        if output is not None:
            shutil.copytree(
                project,
                temporary / "project",
                ignore=shutil.ignore_patterns(".rustyera"),
            )
        if cache is not None:
            (temporary / "compiled-project.reracache").replace(cache)
        if source_index is not None:
            (temporary / "source-index-v1.json").replace(source_index)
        if output is not None:
            if output.exists():
                output.rmdir()
            (temporary / "project").replace(output)
