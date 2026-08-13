"""Bounded terminal-cell canvas used by positioned HTML projection."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rich.cells import cell_len, split_graphemes

from .presentation_types import DisplaySegment, SegmentStyle

MAX_TERMINAL_COLUMNS = 4_096
MAX_TERMINAL_ROWS = 2_048
MAX_TERMINAL_AREA = 1_048_576


@dataclass(slots=True)
class TerminalCanvasCell:
    text: str
    template: DisplaySegment
    lead: int
    width: int
    priority: tuple[int, int]


class TerminalCanvas:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], TerminalCanvasCell] = {}
        self.max_row = -1
        self.max_column = 0
        self._source_order = 0

    def priority(self, depth: int) -> tuple[int, int]:
        self._source_order += 1
        return depth, self._source_order

    def paint_text(
        self,
        row: int,
        column: int,
        text: str,
        template: DisplaySegment,
        clip: tuple[int, int, int, int] | None,
        priority: tuple[int, int],
    ) -> tuple[int, int]:
        line_start = column
        parts = text.split("\n")
        for part_index, part in enumerate(parts):
            spans, _width = split_graphemes(part)
            for start, end, width in spans:
                grapheme = part[start:end]
                if width > 0:
                    self._paint_grapheme(row, column, grapheme, width, template, clip, priority)
                    column += width
            if part_index + 1 < len(parts):
                row += 1
                column = line_start
        return row, column

    def paint_box(
        self,
        top: int,
        left: int,
        height: int,
        width: int,
        context: DisplaySegment,
        background: str | None,
        border_colors: tuple[str | None, str | None, str | None, str | None],
        clip: tuple[int, int, int, int] | None,
        priority: tuple[int, int],
    ) -> None:
        visible = visible_rect(top, left, height, width, clip)
        if visible is None:
            return
        visible_top, visible_left, visible_bottom, visible_right = visible
        if background is not None:
            fill = replace(context, text="", style=replace(context.style, background=background))
            for row in range(visible_top, visible_bottom):
                for column in range(visible_left, visible_right):
                    self._paint_grapheme(row, column, " ", 1, fill, clip, priority)

        bottom = top + height - 1
        right = left + width - 1
        top_color, right_color, bottom_color, left_color = border_colors
        if top_color is not None:
            self._paint_rule(top, left, right, "─", context, top_color, clip, priority)
        if bottom_color is not None:
            self._paint_rule(bottom, left, right, "─", context, bottom_color, clip, priority)
        if left_color is not None:
            self._paint_column(top, bottom, left, "│", context, left_color, clip, priority)
        if right_color is not None:
            self._paint_column(top, bottom, right, "│", context, right_color, clip, priority)
        corners = (
            (top_color, left_color, top, left, "┌"),
            (top_color, right_color, top, right, "┐"),
            (bottom_color, left_color, bottom, left, "└"),
            (bottom_color, right_color, bottom, right, "┘"),
        )
        for horizontal, vertical, row, column, character in corners:
            if horizontal is not None and vertical is not None:
                corner = replace(
                    context,
                    text="",
                    style=replace(context.style, foreground=horizontal),
                )
                self._paint_grapheme(row, column, character, 1, corner, clip, priority)

    def segments(self) -> list[DisplaySegment]:
        if self.max_row < 0:
            return []
        result: list[DisplaySegment] = []
        for row in range(self.max_row + 1):
            row_start = len(result)
            column = 0
            while column < self.max_column:
                cell = self.cells.get((row, column))
                if cell is None:
                    segment = DisplaySegment(" ")
                    width = 1
                elif cell.lead != column:
                    column += 1
                    continue
                else:
                    segment = replace(cell.template, text=cell.text, logical_columns=cell.width)
                    width = cell.width
                if len(result) > row_start and _same_context(result[-1], segment):
                    previous = result[-1]
                    previous_width = previous.logical_columns or cell_len(previous.text)
                    result[-1] = replace(
                        previous,
                        text=previous.text + segment.text,
                        logical_columns=previous_width + width,
                    )
                else:
                    result.append(segment)
                column += width
            while len(result) > row_start and _is_default_padding(result[-1]):
                result.pop()
            if row < self.max_row:
                result.append(DisplaySegment("\n"))
        return result

    def _paint_grapheme(
        self,
        row: int,
        column: int,
        grapheme: str,
        width: int,
        template: DisplaySegment,
        clip: tuple[int, int, int, int] | None,
        priority: tuple[int, int],
    ) -> None:
        if grapheme.isspace() and _is_default_context(template):
            return
        if not inside(row, column, width, clip):
            return
        existing = [self.cells.get((row, occupied)) for occupied in range(column, column + width)]
        if any(cell is not None and cell.priority > priority for cell in existing):
            return
        for cell in existing:
            if cell is not None:
                for previous in range(cell.lead, cell.lead + cell.width):
                    self.cells.pop((row, previous), None)
        painted = TerminalCanvasCell(grapheme, template, column, width, priority)
        for occupied in range(column, column + width):
            self.cells[(row, occupied)] = painted
        self.max_row = max(self.max_row, row)
        self.max_column = max(self.max_column, column + width)

    def _paint_rule(
        self,
        row: int,
        left: int,
        right: int,
        character: str,
        context: DisplaySegment,
        color: str,
        clip: tuple[int, int, int, int] | None,
        priority: tuple[int, int],
    ) -> None:
        template = replace(context, text="", style=replace(context.style, foreground=color))
        for column in range(max(0, left), min(MAX_TERMINAL_COLUMNS, right + 1)):
            self._paint_grapheme(row, column, character, 1, template, clip, priority)

    def _paint_column(
        self,
        top: int,
        bottom: int,
        column: int,
        character: str,
        context: DisplaySegment,
        color: str,
        clip: tuple[int, int, int, int] | None,
        priority: tuple[int, int],
    ) -> None:
        template = replace(context, text="", style=replace(context.style, foreground=color))
        for row in range(max(0, top), min(MAX_TERMINAL_ROWS, bottom + 1)):
            self._paint_grapheme(row, column, character, 1, template, clip, priority)


def intersect_clip(
    parent: tuple[int, int, int, int] | None,
    child: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    if parent is None:
        return child
    top = max(parent[0], child[0])
    left = max(parent[1], child[1])
    return (
        top,
        left,
        max(top, min(parent[2], child[2])),
        max(left, min(parent[3], child[3])),
    )


def visible_rect(
    top: int,
    left: int,
    height: int,
    width: int,
    clip: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    visible = intersect_clip(clip, (top, left, top + height, left + width))
    bounded = intersect_clip(visible, (0, 0, MAX_TERMINAL_ROWS, MAX_TERMINAL_COLUMNS))
    return bounded if bounded[0] < bounded[2] and bounded[1] < bounded[3] else None


def inside(
    row: int,
    column: int,
    width: int,
    clip: tuple[int, int, int, int] | None,
) -> bool:
    if row < 0 or row >= MAX_TERMINAL_ROWS or column < 0 or column + width > MAX_TERMINAL_COLUMNS:
        return False
    return clip is None or (
        clip[0] <= row < clip[2] and clip[1] <= column and column + width <= clip[3]
    )


def _same_context(left: DisplaySegment, right: DisplaySegment) -> bool:
    return replace(left, text="", logical_columns=None) == replace(
        right, text="", logical_columns=None
    )


def _is_default_padding(segment: DisplaySegment) -> bool:
    return segment.text.isspace() and _is_default_context(segment)


def _is_default_context(segment: DisplaySegment) -> bool:
    return (
        segment.style == SegmentStyle()
        and segment.token is None
        and segment.enabled
        and segment.title is None
        and segment.hover_style is None
        and segment.generation is None
        and segment.alignment is None
        and not segment.right_edge
    )
