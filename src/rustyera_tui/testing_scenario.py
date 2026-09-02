"""Private versioned scenario schema parsing for the RuntimeWorker test driver."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .testing_support import TestDriverError

SCENARIO_VERSION = 1
DEFAULT_LIMITS = {"max_steps": 100, "timeout_seconds": 300}


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
        seed = scenario_seed(raw.get("seed"), cls.random_seed) if start_type == "new_game" else None
        inputs = tuple(
            {"value": item} if isinstance(item, (str, int)) else dict(item)
            for item in raw.get("inputs", [])
        )
        rust_only_actions = {"skip_message", "activate_last_button"}
        if any(item.get("action", "input") not in {"input", *rust_only_actions} for item in inputs):
            raise TestDriverError(
                "scenario input action must be input, skip_message, or activate_last_button"
            )
        if any(item.get("action") in rust_only_actions for item in inputs) and raw.get(
            "comparison", {}
        ).get("reference"):
            raise TestDriverError("frontend action scenario inputs cannot be compared by value")
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

    @classmethod
    def random_seed(cls) -> int:
        import secrets

        return secrets.randbelow(0x8000_0000)


def scenario_seed(value: object, random_seed: Any) -> int:
    if value is None:
        return random_seed()
    if type(value) is int:
        seed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        seed = int(value)
    else:
        raise TestDriverError("seed must be a decimal unsigned 64-bit integer")
    if not 0 <= seed <= 0xFFFF_FFFF_FFFF_FFFF:
        raise TestDriverError("seed must be a decimal unsigned 64-bit integer")
    return seed
