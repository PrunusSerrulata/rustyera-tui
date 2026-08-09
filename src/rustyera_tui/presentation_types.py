"""Normalized terminal presentation types and layout constants."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_VIEWPORT_COLUMNS = 100
MIN_TABLE_COLUMN_WIDTH = 16
MAX_TABLE_COLUMN_WIDTH = 24
TARGET_TABLE_COLUMNS = 5


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


@dataclass(frozen=True, slots=True)
class ColumnCellLayout:
    """A semantic PRINTC-family cell over a range of display segments."""

    start: int
    end: int
    alignment: int
    preferred_columns: int


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
