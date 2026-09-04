"""Normalized terminal presentation types and layout constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .text_budget import utf8_length

DEFAULT_VIEWPORT_COLUMNS = 100
MIN_TABLE_COLUMN_WIDTH = 16
MAX_TABLE_COLUMN_WIDTH = 24
TARGET_TABLE_COLUMNS = 5
TERMINAL_CELL_WIDTH_PX = 8
MAX_TERMINAL_PROJECTION_COLUMNS = 4_096


class CellWidthIntent(IntEnum):
    """The unit carried by one canonical PRINTC-family cell width."""

    PROJECT_COLUMNS = 0
    LOGICAL_PIXELS = 1


@dataclass(frozen=True, slots=True)
class SegmentStyle:
    foreground: str = "#d8d8d8"
    background: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False


@dataclass(frozen=True, slots=True)
class DisplaySegment:
    text: str
    style: SegmentStyle = SegmentStyle()
    token: dict[int, int] | None = None
    enabled: bool = True
    title: str | None = None
    hover_style: SegmentStyle | None = None
    generation: int | None = None
    alignment: int | None = None
    right_edge: bool = False
    logical_columns: int | None = None
    interaction_sequence: int | None = None


def segment_interaction_enabled(
    segment: DisplaySegment,
    button_generation: int | None,
    retired_interaction_sequence: int,
) -> bool:
    """Apply frontend-local generation and submission retirement to one segment."""

    if not segment.enabled:
        return False
    if (
        button_generation is not None
        and segment.generation is not None
        and segment.generation != button_generation
    ):
        return False
    return (
        segment.interaction_sequence is None
        or segment.interaction_sequence > retired_interaction_sequence
    )


@dataclass(frozen=True, slots=True)
class ColumnCellLayout:
    """A semantic PRINTC-family cell over a range of display segments."""

    start: int
    end: int
    alignment: int
    width: int
    width_intent: CellWidthIntent = CellWidthIntent.PROJECT_COLUMNS

    @property
    def terminal_columns(self) -> int:
        """Return the deterministic terminal approximation without claiming pixels."""

        if self.width_intent is CellWidthIntent.PROJECT_COLUMNS:
            projected = self.width
        else:
            projected = max(
                1,
                (self.width + TERMINAL_CELL_WIDTH_PX // 2) // TERMINAL_CELL_WIDTH_PX,
            )
        return min(projected, MAX_TERMINAL_PROJECTION_COLUMNS)


@dataclass(frozen=True, slots=True)
class SeparatorLayout:
    """A width-dependent separator inserted before one display-segment index."""

    index: int
    pattern: str


@dataclass(frozen=True, slots=True)
class DisplayLineModel:
    line_id: int
    temporary: bool
    logical_line_start: bool
    line_end: bool
    alignment: int
    segments: tuple[DisplaySegment, ...]
    layout: tuple[ColumnCellLayout | SeparatorLayout, ...] = ()
    text_background_eligible: bool = False
    _retained_utf8_bytes: int = field(init=False, repr=False, compare=False)
    _retained_segments: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        utf8_bytes = 0
        for segment in self.segments:
            utf8_bytes += utf8_length(segment.text)
            if segment.title is not None:
                utf8_bytes += utf8_length(segment.title)
        separator_count = 0
        for item in self.layout:
            if isinstance(item, SeparatorLayout):
                utf8_bytes += utf8_length(item.pattern)
                separator_count += 1
        object.__setattr__(self, "_retained_utf8_bytes", utf8_bytes)
        object.__setattr__(self, "_retained_segments", len(self.segments) + separator_count)
