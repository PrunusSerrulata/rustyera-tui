"""Real, non-Textual replay lifecycle for deterministic performance traces."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO, Callable, TextIO

from .abi import discover_library
from .performance import PerformanceProbe
from .performance_capture import AuditCaptureState
from .client_hello import captured_client_identity
from .performance_process import publish_json_exclusive
from .performance_trace import (
    NORMALIZED_CHECKPOINT_KEYS,
    SNAKE_PROFILE,
    PerformanceTrace,
    PerformanceTraceError,
    canonical_digest,
    freeze_candidate,
    project_digest,
)
from .protocol_text import RUNTIME_PHASES
from .testing import RustTestSession, Scenario, StartSpec, TestDriverError

_WAIT_KINDS = {
    0: "enter_key",
    1: "any_key",
    2: "integer_value",
    3: "string_value",
    4: "void",
    5: "any_value",
    6: "integer_button",
    7: "string_button",
    8: "primitive_mouse_key",
}


class ReplayVerifier:
    """Match every declared expectation before returning the next captured action."""

    def __init__(self, trace: PerformanceTrace) -> None:
        self.trace = trace
        self.index = 0

    @property
    def complete(self) -> bool:
        return self.index == len(self.trace.raw["steps"])

    def verify(self, normalized: dict[str, Any]) -> dict[str, Any]:
        if self.complete:
            raise PerformanceTraceError("replay produced an unexpected extra checkpoint")
        step = self.trace.raw["steps"][self.index]
        expected = step["expect"]
        if set(normalized) != NORMALIZED_CHECKPOINT_KEYS:
            raise PerformanceTraceError("runtime checkpoint is not completely normalized")
        if normalized["phase"] != expected["phase"]:
            raise PerformanceTraceError(f"checkpoint {step['checkpoint']!r} phase drifted")
        wait = normalized["wait"]
        wait_kind = wait.get("kind") if isinstance(wait, dict) else None
        if wait_kind != expected["waitKind"]:
            raise PerformanceTraceError(f"checkpoint {step['checkpoint']!r} wait kind drifted")
        if normalized["otherOutboundTags"] != expected["outboundTags"]:
            raise PerformanceTraceError(f"checkpoint {step['checkpoint']!r} outbound drifted")
        services = [
            {"kind": item.get("kind"), "operation": item.get("operation")}
            for item in normalized["services"]
        ]
        storage = [
            {"namespace": item.get("namespace"), "relativePath": item.get("relativePath")}
            for item in normalized["storage"]
        ]
        if services != expected["services"]:
            raise PerformanceTraceError(f"checkpoint {step['checkpoint']!r} services drifted")
        if storage != expected["storage"]:
            raise PerformanceTraceError(f"checkpoint {step['checkpoint']!r} storage drifted")
        rendered_lines = json.dumps(normalized["lines"], ensure_ascii=False)
        for expected_text in expected["textContains"]:
            if expected_text not in rendered_lines:
                line_digest = canonical_digest({"lines": normalized["lines"]})
                raise PerformanceTraceError(
                    f"checkpoint {step['checkpoint']!r} is missing required text; "
                    f"lines={line_digest}"
                )
        if canonical_digest(normalized) != expected["stateSignature"]:
            raise PerformanceTraceError(
                f"checkpoint {step['checkpoint']!r} state signature mismatch"
            )
        for name, value in expected["variables"].items():
            if normalized["variables"].get(name) != value:
                raise PerformanceTraceError(
                    f"checkpoint {step['checkpoint']!r} variable {name!r} drifted"
                )
        self.index += 1
        return dict(step["action"])

    def reject_terminal_before_match(self, state: str) -> None:
        if not self.complete and state in {"idle", "stopped", "faulted"}:
            step = self.trace.raw["steps"][self.index]
            raise PerformanceTraceError(
                f"runtime became {state} before checkpoint {step['checkpoint']!r}"
            )


class AuditWatchdog:
    """Require changing complete state snapshots more often than five seconds."""

    def __init__(self, *, interval_seconds: float = 4.0) -> None:
        if not 0 < interval_seconds < 5:
            raise ValueError("audit watchdog interval must be below five seconds")
        self.interval_seconds = interval_seconds
        self._last_update = time.monotonic()
        self._last_state: str | None = None

    def refresh(self, normalized: dict[str, Any] | None = None) -> None:
        if normalized is not None:
            digest = canonical_digest(normalized)
            if digest == self._last_state:
                raise TimeoutError("performance replay produced an identical complete state")
            self._last_state = digest
        self._last_update = time.monotonic()

    def check(self) -> None:
        if time.monotonic() - self._last_update > self.interval_seconds:
            raise TimeoutError("performance replay watchdog did not receive a complete state")


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _normalized_lines(session: RustTestSession) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in session.model.lines:
        runs = []
        for segment in line.segments:
            runs.append(
                {
                    "text": segment.text,
                    "style": asdict(segment.style),
                    "enabled": segment.enabled,
                    "title": segment.title,
                    "hoverStyle": (
                        asdict(segment.hover_style) if segment.hover_style is not None else None
                    ),
                    "generation": segment.generation,
                    "alignment": segment.alignment,
                    "rightEdge": segment.right_edge,
                    "logicalColumns": segment.logical_columns,
                }
            )
        result.append(
            {
                "temporary": line.temporary,
                "logicalLineStart": line.logical_line_start,
                "lineEnd": line.line_end,
                "alignment": line.alignment,
                "runs": runs,
                "layout": [_json_value(asdict(item)) for item in line.layout],
                "textBackgroundEligible": line.text_background_eligible,
            }
        )
    return result


def normalize_checkpoint(
    session: RustTestSession,
    observation: dict[str, Any],
    *,
    variables: dict[str, Any],
    transient: dict[str, Any],
) -> dict[str, Any]:
    """Normalize state observable by the real TUI lifecycle, excluding volatile wire IDs."""

    client = session.worker.client
    if client is None:
        raise PerformanceTraceError("runtime client disappeared before checkpoint capture")
    wait = observation.get("wait")
    if not isinstance(wait, dict) or type(wait.get("kind")) is not int:
        raise PerformanceTraceError("runtime checkpoint did not contain a stable input wait")
    try:
        wait_kind = _WAIT_KINDS[wait["kind"]]
    except KeyError as error:
        raise PerformanceTraceError("runtime checkpoint used an unknown wait kind") from error
    return {
        "phase": RUNTIME_PHASES.get(client.phase, f"Unknown({client.phase})"),
        "wait": {
            "kind": wait_kind,
            "stability": wait.get("stability"),
            "systemInput": wait.get("system_input"),
            "timed": wait.get("deadline_ns") is not None,
        },
        "lines": _normalized_lines(session),
        "resources": [],
        "scene": _json_value(session.model.scene),
        "variables": variables,
        "services": transient["services"],
        "storage": transient["storage"],
        "otherOutboundTags": transient["otherOutboundTags"],
    }


def _apply_action(session: RustTestSession, action: dict[str, Any]) -> None:
    kind = action["kind"]
    if kind == "none":
        return
    if kind != "input":
        raise PerformanceTraceError(
            f"TUI headless replay cannot observe or inject {kind!r} at a stable wait"
        )
    if action.get("messageSkip", False):
        session.skip_message()
        return
    intent = action["intent"]
    intent_type = intent["type"]
    if intent_type == "activate":
        session.activate_last_button()
    elif intent_type in {"enter", "continue"}:
        session.submit("")
    elif intent_type in {"any_key", "commit_text", "text", "integer"}:
        session.submit(str(intent.get("value", "")))
    else:
        raise PerformanceTraceError(f"unsupported TUI input intent {intent_type!r}")


def replay_once(
    *,
    trace: PerformanceTrace,
    project: Path,
    runtime_library: Path | None,
    probe: PerformanceProbe,
    verifier: Any,
    watchdog: Any,
    deadline: float,
    pause_at: str | None,
    pause_release: BinaryIO | None,
    pause_flush: Callable[[RustTestSession, dict[str, Any]], None] | None = None,
) -> None:
    """Run one complete trace through RuntimeWorker without constructing a Textual app."""

    scenario = Scenario(
        path=trace.path,
        project=project,
        mode="fixed",
        start=StartSpec("new_game"),
        seed=trace.raw["seed"],
        inputs=(),
        watches=(),
        goal={},
        limits={"max_steps": len(trace.raw["steps"]), "timeout_seconds": 3600},
        comparison={},
        checkpoint=None,
    )
    library = discover_library(runtime_library, project)
    capture = AuditCaptureState()
    session = RustTestSession(
        scenario,
        library,
        project_override=project,
        performance_probe=probe,
        audit_capture=capture,
    )
    try:
        for step in trace.raw["steps"]:
            observation_deadline = min(deadline, time.monotonic() + watchdog.interval_seconds)
            observation = session.wait_observation(observation_deadline)
            watchdog.check()
            expected = step["expect"]
            watches = tuple(expected.get("variables", {}))
            variables = session.inspect(watches, observation_deadline) if watches else {}
            normalized = normalize_checkpoint(
                session,
                observation,
                variables=variables,
                transient=capture.take_interval(),
            )
            watchdog.refresh(normalized)
            action = verifier.verify(normalized)
            probe.record(
                "checkpoint",
                phase="runtime",
                stage="replay",
                operation=step["checkpoint"],
                fields={
                    "stateSignature": expected["stateSignature"],
                    "processId": os.getpid(),
                },
            )
            if pause_at == step["checkpoint"]:
                if pause_flush is not None:
                    pause_flush(session, step)
                if pause_release is None or pause_release.readline() == b"":
                    raise EOFError("profiler checkpoint input closed before release")
            _apply_action(session, action)
        if not verifier.complete:
            verifier.reject_terminal_before_match("idle")
    except TestDriverError as error:
        client = session.worker.client
        state = "stopped" if client is None else RUNTIME_PHASES.get(client.phase, "unknown").lower()
        verifier.reject_terminal_before_match(state)
        raise PerformanceTraceError(str(error)) from error
    finally:
        session.close()


def write_probe_events(stream: BinaryIO, probe: PerformanceProbe) -> None:
    """Flush one epoch to the runner-owned exclusive JSONL stream."""

    for event in probe.drain():
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode())
        stream.write(b"\n")
    stream.flush()


def capture_autonomous(
    *,
    project: Path,
    scenario_name: str,
    seed: int,
    output: Path,
    runtime_library: Path | None,
    deadline: float,
    maximum_steps: int,
    requests: TextIO,
    observations: TextIO,
) -> None:
    """Drive a real headless session from an autonomous controller and freeze its actions."""

    scenario = Scenario(
        path=output,
        project=project,
        mode="autonomous",
        start=StartSpec("new_game"),
        seed=seed,
        inputs=(),
        watches=(),
        goal={},
        limits={"max_steps": maximum_steps, "timeout_seconds": 3600},
        comparison={},
        checkpoint=None,
    )
    capture = AuditCaptureState()
    session = RustTestSession(
        scenario,
        discover_library(runtime_library, project),
        project_override=project,
        audit_capture=capture,
    )
    steps: list[dict[str, Any]] = []
    try:
        for index in range(maximum_steps):
            observation = session.wait_observation(min(deadline, time.monotonic() + 4.0))
            preview = normalize_checkpoint(
                session,
                observation,
                variables={},
                transient={"services": [], "storage": [], "otherOutboundTags": []},
            )
            observations.write(
                json.dumps(
                    {
                        "type": "performance_capture_observation",
                        "step": index,
                        "phase": preview["phase"],
                        "wait": preview["wait"],
                        "lines": preview["lines"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            observations.flush()
            request_line = requests.readline()
            if request_line == "":
                raise EOFError("autonomous capture controller closed before final checkpoint")
            request = json.loads(request_line)
            if not isinstance(request, dict) or set(request) != {
                "id",
                "checkpoint",
                "watches",
                "action",
                "finish",
            }:
                raise PerformanceTraceError("autonomous capture request fields are not exact")
            if not isinstance(request["id"], str) or not request["id"]:
                raise PerformanceTraceError("autonomous capture step requires an id")
            if not isinstance(request["checkpoint"], str) or not request["checkpoint"]:
                raise PerformanceTraceError("autonomous capture step requires a checkpoint")
            if not isinstance(request["watches"], list) or not all(
                isinstance(item, str) for item in request["watches"]
            ):
                raise PerformanceTraceError("autonomous capture watches must be strings")
            if type(request["finish"]) is not bool or not isinstance(request["action"], dict):
                raise PerformanceTraceError("autonomous capture finish/action fields are invalid")
            variables = (
                session.inspect(tuple(request["watches"]), min(deadline, time.monotonic() + 4.0))
                if request["watches"]
                else {}
            )
            normalized = normalize_checkpoint(
                session,
                observation,
                variables=variables,
                transient=capture.take_interval(),
            )
            action = request["action"]
            if request["finish"] and action.get("kind") != "none":
                raise PerformanceTraceError("final autonomous capture action must be none")
            steps.append(
                {
                    "id": request["id"],
                    "checkpoint": request["checkpoint"],
                    "normalized": normalized,
                    "action": action,
                }
            )
            if request["finish"]:
                candidate = {
                    "schemaVersion": 1,
                    "captureRequired": False,
                    "profile": SNAKE_PROFILE,
                    "scenario": scenario_name,
                    "projectDigest": project_digest(project),
                    "seed": seed,
                    "client": captured_client_identity(),
                    "setupMessages": [],
                    "steps": steps,
                }
                freeze_candidate(candidate)
                publish_json_exclusive(output, candidate)
                return
            _apply_action(session, action)
        raise PerformanceTraceError("autonomous capture exhausted --max-steps without final action")
    finally:
        session.close()
