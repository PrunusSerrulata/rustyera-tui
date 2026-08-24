"""Private responsive layout helpers for semantic terminal game lines."""

from __future__ import annotations

from dataclasses import replace

from rich.cells import cell_len, set_cell_size

from .presentation import (
    MAX_TABLE_COLUMN_WIDTH,
    MIN_TABLE_COLUMN_WIDTH,
    TARGET_TABLE_COLUMNS,
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    SeparatorLayout,
)
from .presentation_helpers import segment_columns as _segment_columns

_BOX_RIGHT_CONTINUATIONS = {
    "─": "─",
    "┌": "─",
    "└": "─",
    "├": "─",
    "┬": "─",
    "┴": "─",
    "┼": "─",
    "━": "━",
    "┏": "━",
    "┗": "━",
    "┣": "━",
    "┳": "━",
    "┻": "━",
    "╋": "━",
    "═": "═",
    "╔": "═",
    "╚": "═",
    "╠": "═",
    "╦": "═",
    "╩": "═",
    "╬": "═",
}


def project_responsive_segments(line: DisplayLineModel, width: int) -> tuple[DisplaySegment, ...]:
    if not line.layout:
        return line.segments

    projected: list[DisplaySegment] = []
    segment_index = 0
    layout_index = 0
    while layout_index < len(line.layout):
        item = line.layout[layout_index]
        start = item.start if isinstance(item, ColumnCellLayout) else item.index
        projected.extend(line.segments[segment_index:start])
        segment_index = start

        if isinstance(item, SeparatorLayout):
            separator_width = (
                width
                if item.maximum_columns is None
                else min(width, max(1, item.maximum_columns))
            )
            projected.append(DisplaySegment(separator_text(item.pattern, separator_width)))
            layout_index += 1
            continue

        cells: list[ColumnCellLayout] = [item]
        layout_index += 1
        while layout_index < len(line.layout):
            following = line.layout[layout_index]
            if not isinstance(following, ColumnCellLayout) or following.start != cells[-1].end:
                break
            cells.append(following)
            layout_index += 1
        projected.extend(project_column_group(line.segments, cells, width, projected))
        segment_index = cells[-1].end

    projected.extend(line.segments[segment_index:])
    return tuple(projected)


def project_column_group(
    segments: tuple[DisplaySegment, ...],
    cells: list[ColumnCellLayout],
    width: int,
    preceding: list[DisplaySegment],
) -> list[DisplaySegment]:
    preferred = max(
        MIN_TABLE_COLUMN_WIDTH,
        min(
            MAX_TABLE_COLUMN_WIDTH,
            max(cell.preferred_columns for cell in cells),
        ),
    )
    target_width = MAX_TABLE_COLUMN_WIDTH * TARGET_TABLE_COLUMNS
    if width <= target_width:
        # Compact up to five cells before reducing the row count. This keeps common
        # PRINTC menus dense on narrow terminals without violating the readable minimum.
        capacity = min(
            TARGET_TABLE_COLUMNS,
            max(1, width // MIN_TABLE_COLUMN_WIDTH),
        )
        row_columns = min(len(cells), capacity)
        available_per_column = (
            width // row_columns if width >= MIN_TABLE_COLUMN_WIDTH else MIN_TABLE_COLUMN_WIDTH
        )
        column_width = max(
            MIN_TABLE_COLUMN_WIDTH,
            min(MAX_TABLE_COLUMN_WIDTH, preferred, available_per_column),
        )
    else:
        # Wide viewports add columns only at the maximum width; they never stretch
        # or compact cells merely to consume the trailing remainder.
        column_width = MAX_TABLE_COLUMN_WIDTH
        capacity = max(1, width // column_width)

    result: list[DisplaySegment] = []
    cursor = last_row_width(preceding)
    cells_on_row = 0
    for cell in cells:
        if cells_on_row >= capacity or (cursor > 0 and cursor + column_width > width):
            result.append(DisplaySegment("\n"))
            cursor = 0
            cells_on_row = 0
        content = pad_column_cell(
            segments[cell.start : cell.end],
            cell.alignment,
            column_width,
        )
        content_width = sum(_segment_columns(segment) for segment in content)
        result.extend(content)
        cursor += content_width
        cells_on_row += 1
    return result


def pad_column_cell(
    content: tuple[DisplaySegment, ...],
    alignment: int,
    column_width: int,
) -> list[DisplaySegment]:
    result = list(content)
    content_width = sum(_segment_columns(segment) for segment in result)
    padding = max(0, column_width - content_width)
    if not padding:
        return result
    if not result:
        return [DisplaySegment(" " * padding, logical_columns=padding)]
    edge_index = 0 if alignment == 1 else -1
    edge = result[edge_index]
    result[edge_index] = replace(
        edge,
        text=(" " * padding + edge.text) if alignment == 1 else (edge.text + " " * padding),
        logical_columns=_segment_columns(edge) + padding,
    )
    return result


def separator_text(pattern: str, width: int) -> str:
    pattern = pattern or "-"
    pattern_width = max(1, cell_len(pattern))
    repeated = pattern * (width // pattern_width + 1)
    return set_cell_size(repeated, width)


def last_row_width(segments: list[DisplaySegment]) -> int:
    width = 0
    for segment in reversed(segments):
        if "\n" not in segment.text:
            width += _segment_columns(segment)
            continue
        width += cell_len(segment.text.rsplit("\n", 1)[-1])
        break
    return width


def terminal_segment_text(segment: DisplaySegment) -> str:
    if segment.logical_columns is None or "\n" in segment.text:
        return segment.text
    if segment.logical_columns == 0 and segment.text.isspace():
        return ""
    padding = max(0, segment.logical_columns - cell_len(segment.text))
    if padding and len(segment.text) == 1:
        continuation = _box_drawing_continuation(segment.text)
        if continuation is not None:
            return segment.text + continuation * padding
    return segment.text + " " * padding


def _box_drawing_continuation(character: str) -> str | None:
    """Continue a wide Era box glyph across its narrow terminal cell."""

    return _BOX_RIGHT_CONTINUATIONS.get(character)
