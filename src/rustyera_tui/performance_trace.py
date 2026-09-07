"""Canonical snake-TW performance capture contract shared with Core and Web."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project import FILE_RESOURCE, ProjectBundle
from .client_hello import captured_client_identity

PERFORMANCE_TRACE_SCHEMA_VERSION = 1
SNAKE_PROFILE = "emuera.skia.snake"
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCENARIO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NORMALIZED_CHECKPOINT_KEYS = frozenset(
    {
        "phase",
        "wait",
        "lines",
        "resources",
        "scene",
        "variables",
        "services",
        "storage",
        "otherOutboundTags",
    }
)
_CATEGORY_NAMES = {
    0: "csv",
    1: "erh",
    2: "erb",
    3: "resource_manifest",
    4: "resource",
    5: "configuration",
    6: "als",
    7: "erd",
}
_EXPECT_KEYS = frozenset(
    {
        "phase",
        "waitKind",
        "textContains",
        "outboundTags",
        "services",
        "storage",
        "variables",
        "stateSignature",
    }
)
_TRACE_KEYS = frozenset(
    {
        "schemaVersion",
        "traceDigest",
        "scenario",
        "projectDigest",
        "seed",
        "client",
        "setupMessages",
        "steps",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "schemaVersion",
        "captureRequired",
        "profile",
        "scenario",
        "projectDigest",
        "seed",
        "client",
        "setupMessages",
        "steps",
    }
)
_CANDIDATE_STEP_KEYS = frozenset({"id", "checkpoint", "normalized", "action"})


class PerformanceTraceError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_digest(value: dict[str, Any], *, omit: str | None = None) -> str:
    canonical = {key: item for key, item in value.items() if key != omit}
    return hashlib.sha256(canonical_json(canonical)).hexdigest()


def project_digest(root: Path) -> str:
    """Hash the submitted input stream with the shared Core capture framing."""

    bundle = ProjectBundle.scan(root)
    hasher = hashlib.sha256()
    for item in sorted(bundle.files.values(), key=lambda value: value.relative_path):
        path = item.relative_path.encode("utf-8")
        try:
            category = canonical_json(_CATEGORY_NAMES[item.category])
        except KeyError as error:
            raise PerformanceTraceError(f"unsupported project category {item.category}") from error
        hasher.update(len(path).to_bytes(8, "little"))
        hasher.update(path)
        hasher.update(len(category).to_bytes(8, "little"))
        hasher.update(category)
        if item.category == FILE_RESOURCE:
            source = item.source_path
            if source is None:
                raise PerformanceTraceError(f"resource {item.relative_path!r} has no source path")
            hasher.update(item.content_size.to_bytes(8, "little"))
            with source.open("rb") as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    hasher.update(chunk)
            continue
        payload = item.payload
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or payload[0] != 0
            or not isinstance(payload[1], list)
            or len(payload[1]) != 1
            or not isinstance(payload[1][0], str)
        ):
            raise PerformanceTraceError(f"project input {item.relative_path!r} did not decode")
        content = payload[1][0].encode("utf-8")
        hasher.update(len(content).to_bytes(8, "little"))
        hasher.update(content)
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class PerformanceTrace:
    path: Path
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path, *, scenario: str) -> PerformanceTrace:
        resolved = path.expanduser().resolve(strict=True)
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PerformanceTraceError("performance trace must be a JSON object")
        _validate_trace(raw, scenario=scenario)
        return cls(resolved, raw)


def _validate_trace(raw: dict[str, Any], *, scenario: str) -> None:
    if raw.get("captureRequired") is True:
        raise PerformanceTraceError("capture-required templates cannot be replayed")
    if set(raw) != _TRACE_KEYS:
        raise PerformanceTraceError("frozen trace fields do not match the strict Core contract")
    if raw.get("schemaVersion") != PERFORMANCE_TRACE_SCHEMA_VERSION:
        raise PerformanceTraceError("unsupported performance trace schema")
    if raw.get("scenario") != scenario:
        raise PerformanceTraceError("performance trace scenario does not match --scenario")
    if (
        not isinstance(raw.get("scenario"), str)
        or _SCENARIO_NAME.fullmatch(raw["scenario"]) is None
    ):
        raise PerformanceTraceError("performance trace scenario must be bounded ASCII")
    seed = raw.get("seed")
    if type(seed) is not int or not 0 <= seed <= 0xFFFF_FFFF_FFFF_FFFF:
        raise PerformanceTraceError("performance trace seed must be unsigned 64-bit")
    if raw.get("setupMessages") != []:
        raise PerformanceTraceError("the actual TUI client does not submit setupMessages")
    for field in ("traceDigest", "projectDigest"):
        if not isinstance(raw.get(field), str) or _LOWER_SHA256.fullmatch(raw[field]) is None:
            raise PerformanceTraceError(f"{field} must be lowercase SHA-256 hex")
    expected_digest = canonical_digest(raw, omit="traceDigest")
    if raw["traceDigest"] != expected_digest:
        raise PerformanceTraceError("performance trace digest mismatch")
    client = raw.get("client")
    if not isinstance(client, dict) or not isinstance(client.get("capabilities"), dict):
        raise PerformanceTraceError("performance trace requires captured client capabilities")
    _validate_snake_capabilities(client)
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PerformanceTraceError("performance trace requires captured steps")
    ids: set[str] = set()
    checkpoints: set[str] = set()
    for index, step in enumerate(steps):
        _validate_step(step, index)
        if step["id"] in ids or step["checkpoint"] in checkpoints:
            raise PerformanceTraceError("performance trace step IDs/checkpoints must be unique")
        ids.add(step["id"])
        checkpoints.add(step["checkpoint"])
    if steps[-1]["action"].get("kind") != "none":
        raise PerformanceTraceError("the final performance trace step must use action none")


def _validate_step(step: Any, index: int) -> None:
    if not isinstance(step, dict):
        raise PerformanceTraceError(f"step {index} must be an object")
    if set(step) != {"id", "checkpoint", "expect", "action"}:
        raise PerformanceTraceError(f"step {index} fields do not match the Core contract")
    if not isinstance(step.get("id"), str) or not step["id"]:
        raise PerformanceTraceError(f"step {index} requires an id")
    if not isinstance(step.get("checkpoint"), str) or not step["checkpoint"]:
        raise PerformanceTraceError(f"step {index} requires a checkpoint")
    expect = step.get("expect")
    action = step.get("action")
    if not isinstance(expect, dict) or not isinstance(action, dict):
        raise PerformanceTraceError(f"step {index} requires expect and action objects")
    if set(expect) != _EXPECT_KEYS:
        raise PerformanceTraceError(f"step {index} contains non-Core expectation fields")
    signature = expect.get("stateSignature")
    if not isinstance(signature, str) or _LOWER_SHA256.fullmatch(signature) is None:
        raise PerformanceTraceError(f"step {index} requires a lowercase stateSignature")
    _validate_expect(expect, index)
    _validate_action(action, index)


def _validate_expect(expect: dict[str, Any], index: int) -> None:
    scalar_types = {"phase": str, "waitKind": str, "stateSignature": str}
    for key, expected_type in scalar_types.items():
        if key in expect and not isinstance(expect[key], expected_type):
            raise PerformanceTraceError(f"step {index} expectation {key} has invalid type")
    for key in ("textContains", "outboundTags", "services", "storage"):
        if key in expect and not isinstance(expect[key], list):
            raise PerformanceTraceError(f"step {index} expectation {key} must be a list")
    if "variables" in expect and not isinstance(expect["variables"], dict):
        raise PerformanceTraceError(f"step {index} expectation variables must be an object")
    if not all(isinstance(item, str) for item in expect["textContains"]):
        raise PerformanceTraceError(f"step {index} textContains entries must be strings")
    if not all(type(item) is int and item >= 0 for item in expect["outboundTags"]):
        raise PerformanceTraceError(f"step {index} outboundTags entries must be unsigned integers")
    for service in expect["services"]:
        if not isinstance(service, dict) or set(service) != {"kind", "operation"}:
            raise PerformanceTraceError(f"step {index} service expectation is not exact")
        if not isinstance(service["kind"], str) or not isinstance(service["operation"], str):
            raise PerformanceTraceError(f"step {index} service expectation is invalid")
    for storage in expect["storage"]:
        if not isinstance(storage, dict) or set(storage) != {"namespace", "relativePath"}:
            raise PerformanceTraceError(f"step {index} storage expectation is not exact")
        if not isinstance(storage["namespace"], str) or not isinstance(
            storage["relativePath"], str
        ):
            raise PerformanceTraceError(f"step {index} storage expectation is invalid")
    _validate_json_tree(expect["variables"], f"step {index} variables")


def _validate_action(action: dict[str, Any], index: int) -> None:
    if action.get("kind") not in {"none", "input"}:
        raise PerformanceTraceError(
            f"step {index} action is not executable by the stable-wait TUI replay"
        )
    allowed_action_keys = {
        "none": {"kind"},
        "input": {"kind", "intent", "messageSkip"},
    }[action["kind"]]
    optional_action_keys = {"messageSkip"} if action["kind"] == "input" else set()
    required_action_keys = allowed_action_keys - optional_action_keys
    if not required_action_keys.issubset(action) or not set(action).issubset(allowed_action_keys):
        raise PerformanceTraceError(f"step {index} action fields do not match the Core contract")
    if action["kind"] == "input":
        _validate_input_intent(action["intent"], index)
        if "messageSkip" in action and type(action["messageSkip"]) is not bool:
            raise PerformanceTraceError(f"step {index} messageSkip must be boolean")


def _validate_json_tree(value: Any, location: str) -> None:
    if value is None or type(value) in {bool, int, float, str}:
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{location}[{index}]")
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _validate_json_tree(item, f"{location}.{key}")
        return
    raise PerformanceTraceError(f"{location} is not an exact JSON value")


def _validate_input_intent(intent: Any, index: int) -> None:
    if not isinstance(intent, dict) or not isinstance(intent.get("type"), str):
        raise PerformanceTraceError(f"step {index} input intent must be tagged")
    kind = intent["type"]
    keys = {"type", "value"} if kind == "commit_text" else {"type"}
    if kind not in {"enter", "any_key", "commit_text", "activate", "continue"} or set(
        intent
    ) != keys:
        raise PerformanceTraceError(f"step {index} input intent fields are not exact")
    if "value" in intent:
        _validate_json_tree(intent["value"], f"step {index} input intent value")


def _validate_snake_capabilities(client: dict[str, Any]) -> None:
    if client != captured_client_identity():
        raise PerformanceTraceError("trace client identity differs from the production TUI hello")


def capture_template(*, scenario: str) -> dict[str, Any]:
    """Return a deliberately non-runnable template, never fabricated TW evidence."""

    return {
        "schemaVersion": PERFORMANCE_TRACE_SCHEMA_VERSION,
        "captureRequired": True,
        "profile": SNAKE_PROFILE,
        "scenario": scenario,
        "traceDigest": None,
        "projectDigest": None,
        "seed": None,
        "client": captured_client_identity(),
        "setupMessages": [],
        "steps": [],
    }


def freeze_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a completed capture and derive a strict replay trace without evidence fields."""

    if set(raw) != _CANDIDATE_KEYS:
        raise PerformanceTraceError("capture candidate fields do not match the strict contract")
    if raw.get("schemaVersion") != PERFORMANCE_TRACE_SCHEMA_VERSION:
        raise PerformanceTraceError("unsupported capture candidate schema")
    if raw.get("captureRequired") is not False:
        raise PerformanceTraceError("freeze requires literal captureRequired false")
    if raw.get("profile") != SNAKE_PROFILE:
        raise PerformanceTraceError(f"capture profile must be {SNAKE_PROFILE}")
    scenario = raw.get("scenario")
    if not isinstance(scenario, str) or _SCENARIO_NAME.fullmatch(scenario) is None:
        raise PerformanceTraceError("capture scenario must be bounded ASCII")
    _validate_snake_capabilities(raw.get("client"))
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PerformanceTraceError("completed capture requires steps")
    frozen_steps: list[dict[str, Any]] = []
    for index, candidate in enumerate(steps):
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_STEP_KEYS:
            raise PerformanceTraceError(f"candidate step {index} fields are not exact")
        normalized = candidate["normalized"]
        if not isinstance(normalized, dict) or set(normalized) != NORMALIZED_CHECKPOINT_KEYS:
            raise PerformanceTraceError(f"candidate step {index} is not completely normalized")
        _validate_normalized_checkpoint(normalized, index)
        if not isinstance(candidate["id"], str) or not candidate["id"]:
            raise PerformanceTraceError(f"candidate step {index} requires an id")
        if not isinstance(candidate["checkpoint"], str) or not candidate["checkpoint"]:
            raise PerformanceTraceError(f"candidate step {index} requires a checkpoint")
        action = candidate["action"]
        if not isinstance(action, dict):
            raise PerformanceTraceError(f"candidate step {index} action must be an object")
        _validate_action(action, index)
        frozen_steps.append(
            {
                "id": candidate["id"],
                "checkpoint": candidate["checkpoint"],
                "expect": derive_expectation(normalized),
                "action": action,
            }
        )
    frozen = {
        "schemaVersion": raw["schemaVersion"],
        "traceDigest": "",
        "scenario": scenario,
        "projectDigest": raw.get("projectDigest"),
        "seed": raw.get("seed"),
        "client": raw.get("client"),
        "setupMessages": raw.get("setupMessages"),
        "steps": frozen_steps,
    }
    frozen["traceDigest"] = canonical_digest(frozen, omit="traceDigest")
    _validate_trace(frozen, scenario=scenario)
    return frozen


def derive_expectation(normalized: dict[str, Any]) -> dict[str, Any]:
    """Derive every replay assertion from one canonical normalized checkpoint."""

    if set(normalized) != NORMALIZED_CHECKPOINT_KEYS:
        raise PerformanceTraceError("capture checkpoint is not completely normalized")
    wait = normalized["wait"]
    wait_kind = wait.get("kind") if isinstance(wait, dict) else None
    if not isinstance(wait_kind, str):
        raise PerformanceTraceError("normalized wait requires a string kind")
    phase = normalized["phase"]
    if not isinstance(phase, str):
        raise PerformanceTraceError("normalized phase must be a string")
    return {
        "phase": phase,
        "waitKind": wait_kind,
        "textContains": [],
        "outboundTags": normalized["otherOutboundTags"],
        "services": [
            {"kind": item["kind"], "operation": item["operation"]}
            for item in normalized["services"]
        ],
        "storage": [
            {"namespace": item["namespace"], "relativePath": item["relativePath"]}
            for item in normalized["storage"]
        ],
        "variables": normalized["variables"],
        "stateSignature": canonical_digest(normalized),
    }


def _validate_normalized_checkpoint(normalized: dict[str, Any], index: int) -> None:
    if not isinstance(normalized["phase"], str):
        raise PerformanceTraceError(f"candidate step {index} phase must be a string")
    wait = normalized["wait"]
    if not isinstance(wait, dict) or set(wait) != {
        "kind",
        "stability",
        "systemInput",
        "timed",
    }:
        raise PerformanceTraceError(f"candidate step {index} wait fields are not exact")
    if not isinstance(wait["kind"], str) or type(wait["timed"]) is not bool:
        raise PerformanceTraceError(f"candidate step {index} wait fields are invalid")
    if normalized["resources"] != []:
        raise PerformanceTraceError("the actual TUI capture cannot claim retained resources")
    if not isinstance(normalized["lines"], list):
        raise PerformanceTraceError(f"candidate step {index} lines must be a list")
    line_keys = {
        "temporary",
        "logicalLineStart",
        "lineEnd",
        "alignment",
        "runs",
        "layout",
        "textBackgroundEligible",
    }
    run_keys = {
        "text",
        "style",
        "enabled",
        "title",
        "hoverStyle",
        "generation",
        "alignment",
        "rightEdge",
        "logicalColumns",
    }
    style_keys = {"foreground", "background", "bold", "italic", "underline", "strike"}
    for line in normalized["lines"]:
        if not isinstance(line, dict) or set(line) != line_keys:
            raise PerformanceTraceError(f"candidate step {index} line fields are not exact")
        if not isinstance(line["runs"], list) or not isinstance(line["layout"], list):
            raise PerformanceTraceError(f"candidate step {index} line collections are invalid")
        for run in line["runs"]:
            if not isinstance(run, dict) or set(run) != run_keys:
                raise PerformanceTraceError(f"candidate step {index} run fields are not exact")
            if not isinstance(run["style"], dict) or set(run["style"]) != style_keys:
                raise PerformanceTraceError(f"candidate step {index} style fields are not exact")
            hover = run["hoverStyle"]
            if hover is not None and (not isinstance(hover, dict) or set(hover) != style_keys):
                raise PerformanceTraceError(f"candidate step {index} hover style is not exact")
        for layout in line["layout"]:
            if not isinstance(layout, dict) or set(layout) not in (
                {"start", "end", "alignment", "width", "width_intent"},
                {"index", "pattern"},
            ):
                raise PerformanceTraceError(f"candidate step {index} layout fields are not exact")
    if not isinstance(normalized["variables"], dict):
        raise PerformanceTraceError(f"candidate step {index} variables must be an object")
    _validate_json_tree(normalized["scene"], f"candidate step {index} scene")
    _validate_json_tree(normalized["variables"], f"candidate step {index} variables")
    if not isinstance(normalized["services"], list):
        raise PerformanceTraceError(f"candidate step {index} services must be a list")
    for service in normalized["services"]:
        if not isinstance(service, dict) or set(service) != {
            "kind",
            "operation",
            "operationVersion",
            "payload",
        }:
            raise PerformanceTraceError(f"candidate step {index} service payload is not exact")
        versions = service["operationVersion"]
        if not isinstance(versions, dict) or set(versions) != {"minimum", "maximum"}:
            raise PerformanceTraceError(f"candidate step {index} service version is not exact")
        for bound in versions.values():
            if not isinstance(bound, dict) or set(bound) != {"major", "minor"}:
                raise PerformanceTraceError(
                    f"candidate step {index} service version bound is not exact"
                )
        _validate_json_tree(service, f"candidate step {index} service")
    if not isinstance(normalized["storage"], list):
        raise PerformanceTraceError(f"candidate step {index} storage must be a list")
    for storage in normalized["storage"]:
        if not isinstance(storage, dict) or set(storage) != {
            "namespace",
            "relativePath",
            "operation",
        }:
            raise PerformanceTraceError(f"candidate step {index} storage payload is not exact")
        operation = storage["operation"]
        operation_fields = {
            "read": {"type"},
            "write": {"type", "data", "atomic_replace", "precondition"},
            "list": {"type", "pattern", "recursive"},
            "delete": {"type", "precondition"},
            "stat": {"type"},
            "read_range": {"type", "offset", "maximum_bytes", "change_token"},
        }
        if (
            not isinstance(operation, dict)
            or operation.get("type") not in operation_fields
            or set(operation) != operation_fields[operation["type"]]
        ):
            raise PerformanceTraceError(
                f"candidate step {index} storage operation fields are not exact"
            )
        _validate_json_tree(storage, f"candidate step {index} storage")
    if not isinstance(normalized["otherOutboundTags"], list) or not all(
        type(tag) is int and tag >= 0 for tag in normalized["otherOutboundTags"]
    ):
        raise PerformanceTraceError(f"candidate step {index} outbound tags are invalid")
