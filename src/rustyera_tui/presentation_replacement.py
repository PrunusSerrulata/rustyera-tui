"""Shared replacement-boundary state for retiring one game's presentation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

CLEAR_HISTORY_OPERATION = 2
RETIRED_HISTORY_OPERATIONS = frozenset({0, 1, 6, 7, 14})


class ReplacementPhase(Enum):
    IDLE = auto()
    RETIRED = auto()
    RESYNC = auto()


@dataclass(slots=True)
class ReplacementBoundary:
    phase: ReplacementPhase = ReplacementPhase.IDLE
    command_message_id: int | None = None

    @property
    def active(self) -> bool:
        return self.phase is not ReplacementPhase.IDLE

    def begin(self, command_message_id: int | None = None) -> None:
        self.phase = ReplacementPhase.RETIRED
        self.command_message_id = command_message_id

    def accepts_operation(self, tag: int) -> bool:
        if not self.active:
            return True
        if tag == CLEAR_HISTORY_OPERATION:
            self.clear()
            return True
        return tag not in RETIRED_HISTORY_OPERATIONS

    def accept_snapshot(self) -> None:
        self.clear()

    def reject(self, correlation_id: int | None) -> bool:
        if (
            self.phase is not ReplacementPhase.RETIRED
            or correlation_id is None
            or correlation_id != self.command_message_id
        ):
            return False
        self.phase = ReplacementPhase.RESYNC
        self.command_message_id = None
        return True

    def clear(self) -> None:
        self.phase = ReplacementPhase.IDLE
        self.command_message_id = None
