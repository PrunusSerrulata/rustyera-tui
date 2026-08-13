"""Queue messages shared by the runtime client, worker, and Textual app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DiagnosisProgressStage = Literal[
    "waiting",
    "input_replay",
    "vm_snapshot",
    "project_scanning",
    "project_preparing",
    "project_packaging",
    "project_transfer",
    "archive",
]


@dataclass(frozen=True, slots=True)
class FrontendEvent:
    kind: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class DiagnosisProgress:
    """One observable unit of the frontend-owned diagnosis export pipeline."""

    stage: DiagnosisProgressStage
    completed: int = 0
    total: int = 0


@dataclass(frozen=True, slots=True)
class PresentationBatch:
    """One worker-side presentation observation delivered atomically to Textual."""

    snapshot: dict[int, Any] | None
    delta: dict[int, Any] | None
    active_wait: dict[int, Any] | None
    render: bool


@dataclass(frozen=True, slots=True)
class FrontendCommand:
    kind: str
    value: Any = None
