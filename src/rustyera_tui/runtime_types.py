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
class GameInformation:
    """User-facing metadata projected from the active project's GameBase.csv."""

    title: str | None = None
    author: str | None = None
    version: str | None = None
    year: str | None = None
    information: str | None = None

    @classmethod
    def from_wire(cls, value: Any) -> GameInformation:
        if not isinstance(value, dict):
            return cls()

        def text(key: int) -> str | None:
            item = value.get(key)
            return item if isinstance(item, str) and item.strip() else None

        return cls(text(0), text(1), text(2), text(3), text(4))

    def display_items(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (label, value)
            for label, value in (
                ("游戏名称", self.title),
                ("游戏作者", self.author),
                ("游戏版本", self.version),
                ("游戏开发时间", self.year),
                ("备注", self.information),
            )
            if value is not None
        )


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
