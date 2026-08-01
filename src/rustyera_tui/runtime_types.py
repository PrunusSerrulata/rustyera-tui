"""Queue messages shared by the runtime client, worker, and Textual app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FrontendEvent:
    kind: str
    value: Any = None


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
