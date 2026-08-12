"""Deterministic terminal projection for Emuera positioned HTML and shapes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any, Callable, cast

from rich.cells import cell_len, split_graphemes

from .presentation_types import DisplaySegment, SegmentStyle
from .wire import unwrap_variant

TERMINAL_CELL_WIDTH_PX = 8
TERMINAL_CELL_HEIGHT_PX = 16
MAX_TERMINAL_COLUMNS = 4_096
MAX_TERMINAL_ROWS = 2_048
MAX_TERMINAL_AREA = 1_048_576


class _LengthDomain(Enum):
    HTML = auto()
    PRESENTATION = auto()


def project_direct_shape(shape: Any, context: DisplaySegment) -> list[DisplaySegment]:
    """Project a protocol Shape, where variant zero is a logical milli-pixel."""

    return _project_rectangle(shape, context, domain=_LengthDomain.PRESENTATION)


def project_html_shape(semantic: Any, context: DisplaySegment) -> list[DisplaySegment]:
    """Project an HTML shape semantic, where variant zero is a physical pixel."""

    kind = _semantic_field(semantic, 0, "")
    parameters = _semantic_field(semantic, 1, [])
    if str(kind).lower() == "space" and parameters:
        columns = _project_length(
            parameters[0], horizontal=True, domain=_LengthDomain.HTML
        )
        if columns is None or not 0 <= columns <= MAX_TERMINAL_COLUMNS:
            return [replace(context, text="[图形]")]
        return [replace(context, text=" " * columns, logical_columns=columns)]
    shape = {
        0: kind,
        1: parameters,
        2: _semantic_field(semantic, 2),
        3: _semantic_field(semantic, 3),
    }
    return _project_rectangle(shape, context, domain=_LengthDomain.HTML)


def has_relative_division(node: list[Any]) -> bool:
    try:
        tag, fields = unwrap_variant(node)
    except (TypeError, ValueError):
        return False
    if tag != 1 or len(fields) < 3 or not isinstance(fields[2], list):
        return False
    semantic = fields[6] if len(fields) > 6 else None
    if fields[0] == 12 and _semantic_field(semantic, 6, False) is True:
        return True
    return any(has_relative_division(child) for child in fields[2])


def project_positioned_html(
    nodes: list[Any],
    base: DisplaySegment,
    *,
    font_context: Callable[[Any, DisplaySegment], DisplaySegment],
    button_context: Callable[[Any, dict[int, Any] | None, DisplaySegment], DisplaySegment],
) -> list[DisplaySegment]:
    """Flatten relative divisions to a bounded 8x16-pixel terminal cell canvas."""

    canvas = _TerminalCanvas()
    cursor = _HtmlCursor(0, 0, 0)
    for node in nodes:
        _paint_html_node(
            node,
            base,
            cursor,
            canvas,
            None,
            0,
            font_context,
            button_context,
        )
    return canvas.segments()


def _project_rectangle(
    shape: Any,
    context: DisplaySegment,
    *,
    domain: _LengthDomain,
) -> list[DisplaySegment]:
    if not isinstance(shape, dict) or str(shape.get(0, "")).lower() != "rect":
        return [replace(context, text="[图形]")]
    parameters = shape.get(1, [])
    if not isinstance(parameters, list) or len(parameters) not in (1, 4):
        return [replace(context, text="[图形]")]
    if len(parameters) == 1:
        if not _raw_length_positive(parameters[0]):
            return [replace(context, text="[图形]")]
        x, y, height = 0, 0, 1
        width = _project_length(
            parameters[0], horizontal=True, domain=domain, extent=True
        )
    else:
        if not _raw_length_positive(parameters[2]) or not _raw_length_positive(
            parameters[3]
        ):
            return [replace(context, text="[图形]")]
        x = _project_length(parameters[0], horizontal=True, domain=domain)
        y = _project_length(parameters[1], horizontal=False, domain=domain)
        width = _project_length(
            parameters[2], horizontal=True, domain=domain, extent=True
        )
        height = _project_length(
            parameters[3], horizontal=False, domain=domain, extent=True
        )
    if (
        x is None
        or y is None
        or width is None
        or height is None
        or x < 0
        or width <= 0
        or height <= 0
        or abs(y) > MAX_TERMINAL_ROWS
        or x + width > MAX_TERMINAL_COLUMNS
        or height > MAX_TERMINAL_ROWS
    ):
        return [replace(context, text="[图形]")]

    style = context.style
    foreground = _shape_color(shape.get(2))
    if foreground is not None:
        style = replace(style, foreground=foreground)
    hover_style = context.hover_style
    button_color = _shape_color(shape.get(3))
    if button_color is not None:
        hover_style = replace(style, foreground=button_color)
    result: list[DisplaySegment] = []
    if x:
        result.append(replace(context, text=" " * x, logical_columns=x))
    result.append(
        replace(
            context,
            text="━" * width,
            logical_columns=width,
            style=style,
            hover_style=hover_style,
        )
    )
    return result


@dataclass(slots=True)
class _CanvasCell:
    text: str
    template: DisplaySegment
    lead: int
    width: int
    priority: tuple[int, int]


class _TerminalCanvas:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], _CanvasCell] = {}
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
                    self._paint_grapheme(
                        row, column, grapheme, width, template, clip, priority
                    )
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
        visible = _visible_rect(top, left, height, width, clip)
        if visible is None:
            return
        visible_top, visible_left, visible_bottom, visible_right = visible
        if background is not None:
            fill = replace(
                context,
                text="",
                style=replace(context.style, background=background),
            )
            for row in range(visible_top, visible_bottom):
                for column in range(visible_left, visible_right):
                    self._paint_grapheme(
                        row, column, " ", 1, fill, clip, priority
                    )

        bottom = top + height - 1
        right = left + width - 1
        top_color, right_color, bottom_color, left_color = border_colors
        if top_color is not None:
            self._paint_rule(
                top, left, right, "─", context, top_color, clip, priority
            )
        if bottom_color is not None:
            self._paint_rule(
                bottom, left, right, "─", context, bottom_color, clip, priority
            )
        if left_color is not None:
            self._paint_column(
                top, bottom, left, "│", context, left_color, clip, priority
            )
        if right_color is not None:
            self._paint_column(
                top, bottom, right, "│", context, right_color, clip, priority
            )
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
                self._paint_grapheme(
                    row, column, character, 1, corner, clip, priority
                )

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
                    segment = replace(
                        cell.template,
                        text=cell.text,
                        logical_columns=cell.width,
                    )
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
        if not _inside(row, column, width, clip):
            return
        existing = [self.cells.get((row, occupied)) for occupied in range(column, column + width)]
        if any(cell is not None and cell.priority > priority for cell in existing):
            return
        for cell in existing:
            if cell is not None:
                for previous in range(cell.lead, cell.lead + cell.width):
                    self.cells.pop((row, previous), None)
        painted = _CanvasCell(grapheme, template, column, width, priority)
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
        template = replace(
            context,
            text="",
            style=replace(context.style, foreground=color),
        )
        for column in range(max(0, left), min(MAX_TERMINAL_COLUMNS, right + 1)):
            self._paint_grapheme(
                row, column, character, 1, template, clip, priority
            )

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
        template = replace(
            context,
            text="",
            style=replace(context.style, foreground=color),
        )
        for row in range(max(0, top), min(MAX_TERMINAL_ROWS, bottom + 1)):
            self._paint_grapheme(
                row, column, character, 1, template, clip, priority
            )


@dataclass(slots=True)
class _HtmlCursor:
    row: int
    column: int
    line_start: int


def _paint_html_node(
    node: list[Any],
    context: DisplaySegment,
    cursor: _HtmlCursor,
    canvas: _TerminalCanvas,
    clip: tuple[int, int, int, int] | None,
    depth: int,
    font_context: Callable[[Any, DisplaySegment], DisplaySegment],
    button_context: Callable[[Any, dict[int, Any] | None, DisplaySegment], DisplaySegment],
) -> None:
    try:
        tag, fields = unwrap_variant(node)
    except (TypeError, ValueError):
        return
    if tag == 0 and fields:
        cursor.row, cursor.column = canvas.paint_text(
            cursor.row,
            cursor.column,
            str(fields[0]),
            context,
            clip,
            canvas.priority(depth),
        )
        return
    if tag != 1 or len(fields) < 3 or not isinstance(fields[2], list):
        return
    kind, children = fields[0], fields[2]
    interaction = fields[3] if len(fields) > 3 else None
    semantic = fields[6] if len(fields) > 6 else None
    if kind == 13:
        cursor.row += 1
        cursor.column = cursor.line_start
        return
    if kind == 10:  # Terminal clients intentionally omit images.
        return
    if kind == 11:
        for segment in project_html_shape(semantic, context):
            cursor.row, cursor.column = canvas.paint_text(
                cursor.row,
                cursor.column,
                segment.text,
                segment,
                clip,
                canvas.priority(depth),
            )
        return
    if kind == 12 and _semantic_field(semantic, 6, False) is True:
        _paint_relative_division(
            semantic,
            children,
            context,
            cursor,
            canvas,
            clip,
            depth,
            font_context,
            button_context,
        )
        return

    nested = context
    if kind == 0:
        nested = replace(nested, style=replace(nested.style, bold=True))
    elif kind == 1:
        nested = replace(nested, style=replace(nested.style, italic=True))
    elif kind == 2:
        nested = replace(nested, style=replace(nested.style, underline=True))
    elif kind == 3:
        nested = replace(nested, style=replace(nested.style, strike=True))
    elif kind == 4:
        nested = font_context(semantic, nested)
    elif kind == 5:
        nested = replace(nested, alignment=int(_semantic_field(semantic, 0, 0)))
    elif kind == 7:
        nested = button_context(semantic, interaction, nested)
    elif kind in (8, 9):
        nested = replace(
            nested,
            token=None,
            enabled=True,
            title=None,
            hover_style=None,
            generation=None,
        )
    for child in children:
        _paint_html_node(
            child,
            nested,
            cursor,
            canvas,
            clip,
            depth,
            font_context,
            button_context,
        )


def _paint_relative_division(
    semantic: Any,
    children: list[Any],
    context: DisplaySegment,
    anchor: _HtmlCursor,
    canvas: _TerminalCanvas,
    parent_clip: tuple[int, int, int, int] | None,
    parent_depth: int,
    font_context: Callable[[Any, DisplaySegment], DisplaySegment],
    button_context: Callable[[Any, dict[int, Any] | None, DisplaySegment], DisplaySegment],
) -> None:
    x_value = _semantic_field(semantic, 0)
    y_value = _semantic_field(semantic, 1)
    width_value = _semantic_field(semantic, 2)
    height_value = _semantic_field(semantic, 3)
    if not _raw_length_positive(width_value) or not _raw_length_positive(height_value):
        return
    x = _project_length(
        x_value,
        horizontal=True,
        domain=_LengthDomain.HTML,
    )
    y = _project_length(
        y_value,
        horizontal=False,
        domain=_LengthDomain.HTML,
    )
    width = _project_length(
        width_value,
        horizontal=True,
        domain=_LengthDomain.HTML,
        extent=True,
    )
    height = _project_length(
        height_value,
        horizontal=False,
        domain=_LengthDomain.HTML,
        extent=True,
    )
    declared_depth = _bounded_depth(_semantic_field(semantic, 4, 0))
    if (
        x is None
        or y is None
        or width is None
        or height is None
        or declared_depth is None
    ):
        return
    box_model = _semantic_field(semantic, 7, {})
    margin = _project_box_sides(box_model.get(2) if isinstance(box_model, dict) else None)
    padding = _project_box_sides(box_model.get(3) if isinstance(box_model, dict) else None)
    border_values = box_model.get(0) if isinstance(box_model, dict) else None
    border = _project_border_sides(border_values)
    top = anchor.row + y + margin[0]
    left = anchor.column + x + margin[3]
    width -= margin[1] + margin[3]
    height -= margin[0] + margin[2]
    if (
        width <= 0
        or height <= 0
        or width > MAX_TERMINAL_COLUMNS
        or height > MAX_TERMINAL_ROWS
        or width * height > MAX_TERMINAL_AREA
        or not -MAX_TERMINAL_ROWS <= top <= MAX_TERMINAL_ROWS
        or not -MAX_TERMINAL_COLUMNS <= left <= MAX_TERMINAL_COLUMNS
    ):
        return
    own_clip = _intersect_clip(parent_clip, (top, left, top + height, left + width))
    layer_depth = parent_depth + declared_depth
    if not -32_768 <= layer_depth <= 32_767:
        return
    colors = box_model.get(4) if isinstance(box_model, dict) else None
    border_colors = _border_colors(border, colors, context.style.foreground)
    background_value = _semantic_field(semantic, 5)
    background = (
        _packed_color_hex(background_value)
        if isinstance(background_value, int)
        else None
    )
    canvas.paint_box(
        top,
        left,
        height,
        width,
        context,
        background,
        border_colors,
        parent_clip,
        canvas.priority(layer_depth),
    )
    content_top = top + border[0] + padding[0]
    content_left = left + border[3] + padding[3]
    child_cursor = _HtmlCursor(content_top, content_left, content_left)
    for child in children:
        _paint_html_node(
            child,
            context,
            child_cursor,
            canvas,
            own_clip,
            layer_depth,
            font_context,
            button_context,
        )


def _project_length(
    value: Any,
    *,
    horizontal: bool,
    domain: _LengthDomain,
    extent: bool = False,
) -> int | None:
    if value is None:
        return None
    try:
        unit, fields = unwrap_variant(value)
        raw_value = fields[0]
        raw = raw_value[0] if isinstance(raw_value, list) else raw_value
        raw = int(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if unit == 0:
        divisor = TERMINAL_CELL_WIDTH_PX if horizontal else TERMINAL_CELL_HEIGHT_PX
        if domain is _LengthDomain.PRESENTATION:
            divisor *= 1_000
    elif unit == 1:
        divisor = 50 if horizontal else 100
    else:
        return None
    projected = _round_away_from_zero(raw, divisor)
    if extent and raw != 0:
        projected = max(1, abs(projected))
    limit = MAX_TERMINAL_COLUMNS if horizontal else MAX_TERMINAL_ROWS
    return projected if -limit <= projected <= limit else None


def _project_box_sides(values: Any) -> tuple[int, int, int, int]:
    if not isinstance(values, list) or len(values) != 4:
        return 0, 0, 0, 0
    axes = (False, True, False, True)
    projected = [
        _project_length(
            value,
            horizontal=horizontal,
            domain=_LengthDomain.HTML,
        )
        for value, horizontal in zip(values, axes, strict=True)
    ]
    return cast(
        tuple[int, int, int, int],
        tuple(max(0, value or 0) for value in projected),
    )


def _project_border_sides(values: Any) -> tuple[int, int, int, int]:
    if not isinstance(values, list) or len(values) != 4:
        return 0, 0, 0, 0
    top, right, bottom, left = (_raw_length_positive(value) for value in values)
    return int(top), int(right), int(bottom), int(left)


def _border_colors(
    borders: tuple[int, int, int, int],
    colors: Any,
    fallback: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    packed = colors if isinstance(colors, list) and len(colors) == 4 else None
    result: list[str | None] = []
    for index, present in enumerate(borders):
        if not present:
            result.append(None)
        elif packed is not None and isinstance(packed[index], int):
            result.append(_packed_color_hex(packed[index]))
        else:
            result.append(fallback)
    return result[0], result[1], result[2], result[3]


def _raw_length_positive(value: Any) -> bool:
    try:
        _tag, fields = unwrap_variant(value)
        raw_value = fields[0]
        raw = raw_value[0] if isinstance(raw_value, list) else raw_value
        return int(raw) > 0
    except (TypeError, ValueError, IndexError):
        return False


def _bounded_depth(value: Any) -> int | None:
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return None
    return depth if -32_768 <= depth <= 32_767 else None


def _visible_rect(
    top: int,
    left: int,
    height: int,
    width: int,
    clip: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    visible = _intersect_clip(
        clip,
        (top, left, top + height, left + width),
    )
    bounded = _intersect_clip(
        visible,
        (0, 0, MAX_TERMINAL_ROWS, MAX_TERMINAL_COLUMNS),
    )
    return bounded if bounded[0] < bounded[2] and bounded[1] < bounded[3] else None


def _inside(
    row: int,
    column: int,
    width: int,
    clip: tuple[int, int, int, int] | None,
) -> bool:
    if (
        row < 0
        or row >= MAX_TERMINAL_ROWS
        or column < 0
        or column + width > MAX_TERMINAL_COLUMNS
    ):
        return False
    return clip is None or (
        clip[0] <= row < clip[2]
        and clip[1] <= column
        and column + width <= clip[3]
    )


def _intersect_clip(
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


def _round_away_from_zero(value: int, divisor: int) -> int:
    magnitude = (abs(value) + divisor // 2) // divisor
    return magnitude if value >= 0 else -magnitude


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


def _semantic_field(semantic: Any, index: int, default: Any = None) -> Any:
    if semantic is None:
        return default
    try:
        _tag, fields = unwrap_variant(semantic)
    except (TypeError, ValueError):
        return default
    return fields[index] if index < len(fields) else default


def _shape_color(value: Any) -> str | None:
    if isinstance(value, dict):
        return _color_hex(value)
    if isinstance(value, int):
        return _packed_color_hex(value)
    return None


def _color_hex(color: dict[int, int]) -> str:
    return f"#{color.get(0, 0):02x}{color.get(1, 0):02x}{color.get(2, 0):02x}"


def _packed_color_hex(value: int) -> str:
    return f"#{(value >> 16) & 0xFF:02x}{(value >> 8) & 0xFF:02x}{value & 0xFF:02x}"
