"""Deterministic terminal projection for Emuera positioned HTML and shapes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any, Callable, cast

from .presentation_helpers import (
    packed_color_hex as _packed_color_hex,
    semantic_field as _semantic_field,
)
from .presentation_types import DisplaySegment
from .terminal_canvas import (
    MAX_TERMINAL_AREA,
    MAX_TERMINAL_COLUMNS,
    MAX_TERMINAL_ROWS,
    TerminalCanvas,
    intersect_clip,
)
from .wire import unwrap_variant

TERMINAL_CELL_WIDTH_PX = 8
TERMINAL_CELL_HEIGHT_PX = 16


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
        columns = _project_length(parameters[0], horizontal=True, domain=_LengthDomain.HTML)
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

    canvas = TerminalCanvas()
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
        width = _project_length(parameters[0], horizontal=True, domain=domain, extent=True)
    else:
        if not _raw_length_positive(parameters[2]) or not _raw_length_positive(parameters[3]):
            return [replace(context, text="[图形]")]
        x = _project_length(parameters[0], horizontal=True, domain=domain)
        y = _project_length(parameters[1], horizontal=False, domain=domain)
        width = _project_length(parameters[2], horizontal=True, domain=domain, extent=True)
        height = _project_length(parameters[3], horizontal=False, domain=domain, extent=True)
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
class _HtmlCursor:
    row: int
    column: int
    line_start: int


def _paint_html_node(
    node: list[Any],
    context: DisplaySegment,
    cursor: _HtmlCursor,
    canvas: TerminalCanvas,
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
    canvas: TerminalCanvas,
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
    if x is None or y is None or width is None or height is None or declared_depth is None:
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
    own_clip = intersect_clip(parent_clip, (top, left, top + height, left + width))
    layer_depth = parent_depth + declared_depth
    if not -32_768 <= layer_depth <= 32_767:
        return
    colors = box_model.get(4) if isinstance(box_model, dict) else None
    border_colors = _border_colors(border, colors, context.style.foreground)
    background_value = _semantic_field(semantic, 5)
    background = _packed_color_hex(background_value) if isinstance(background_value, int) else None
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


def _round_away_from_zero(value: int, divisor: int) -> int:
    magnitude = (abs(value) + divisor // 2) // divisor
    return magnitude if value >= 0 else -magnitude


def _shape_color(value: Any) -> str | None:
    if isinstance(value, dict):
        return _color_hex(value)
    if isinstance(value, int):
        return _packed_color_hex(value)
    return None


def _color_hex(color: dict[int, int]) -> str:
    return f"#{color.get(0, 0):02x}{color.get(1, 0):02x}{color.get(2, 0):02x}"
