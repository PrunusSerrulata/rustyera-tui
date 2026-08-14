"""Projection of normalized runtime runs into terminal display spans and service text."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .presentation_helpers import (
    packed_color_hex as _packed_color_hex,
    segment_columns as _segment_columns,
    semantic_field as _semantic_field,
)
from .presentation_text import (
    html_printed_str as html_printed_str,
    plain_line as plain_line,
    plain_run as plain_run,
)
from .presentation_types import (
    DEFAULT_VIEWPORT_COLUMNS,
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    SegmentStyle,
    SeparatorLayout,
)
from .terminal_html_layout import (
    has_relative_division,
    project_direct_shape,
    project_html_shape,
    project_positioned_html,
)
from .wire import unwrap_variant

SAVE_DELETE_PATTERN = re.compile(r"Delete save\d+\.sav")


def color_hex(color: dict[int, int] | None) -> str:
    if not color:
        return "#000000"
    return f"#{color.get(0, 0):02x}{color.get(1, 0):02x}{color.get(2, 0):02x}"


def parse_style(style: dict[int, Any] | None) -> SegmentStyle:
    if not style:
        return SegmentStyle()
    return SegmentStyle(
        foreground=color_hex(style.get(0)),
        background=color_hex(style[1]) if style.get(1) is not None else None,
        bold=bool(style.get(2, False)),
        italic=bool(style.get(3, False)),
        underline=bool(style.get(4, False)),
        strike=bool(style.get(5, False)),
    )


def parse_line(line: dict[int, Any]) -> DisplayLineModel:
    segments: list[DisplaySegment] = []
    layout: list[ColumnCellLayout | SeparatorLayout] = []
    for run in line.get(5, []):
        tag, fields = unwrap_variant(run)
        if tag == 5:  # PRINTC-family semantic cell
            content, alignment, preferred_columns = fields
            start = len(segments)
            for child in content:
                segments.extend(parse_run(child))
            layout.append(
                ColumnCellLayout(start, len(segments), int(alignment), int(preferred_columns))
            )
        elif tag == 6:  # Width-independent separator
            layout.append(SeparatorLayout(len(segments), fields[0] or "-"))
        else:
            segments.extend(parse_run(run))
    return DisplayLineModel(
        line_id=line[0],
        temporary=line.get(1, False),
        logical_line_start=line.get(2, True),
        line_end=line.get(3, True),
        alignment=line.get(4, 0),
        segments=tuple(segments),
        layout=tuple(layout),
    )


def parse_run(run: list[Any], inherited: DisplaySegment | None = None) -> list[DisplaySegment]:
    tag, fields = unwrap_variant(run)
    if tag in (0, 8):  # Canonical text or runtime-owned frontend text layout
        text, style = fields[0], parse_style(fields[1])
        return [
            DisplaySegment(
                text=text,
                logical_columns=max(0, int(fields[3])) if tag == 8 else None,
                style=style,
                token=inherited.token if inherited else None,
                enabled=inherited.enabled if inherited else True,
                title=inherited.title if inherited else None,
                hover_style=inherited.hover_style if inherited else None,
                generation=inherited.generation if inherited else None,
            )
        ]
    if tag == 1:  # Button
        runs, token, title, hover_style = fields[:4]
        button = DisplaySegment(
            text="",
            token=token,
            enabled=bool(fields[6]),
            title=title,
            hover_style=parse_style(hover_style) if hover_style else None,
            generation=int(fields[5]),
        )
        result: list[DisplaySegment] = []
        for child in runs:
            result.extend(parse_run(child, button))
        button_text = "".join(segment.text for segment in result)
        if SAVE_DELETE_PATTERN.fullmatch(button_text) and any(
            _run_has_system_text_key(child, 9) for child in runs
        ):
            source_style = result[0].style if result else SegmentStyle()
            return [
                replace(
                    button,
                    text="[X]",
                    style=replace(source_style, foreground="#ef4444", bold=True),
                    title=title or button_text,
                    right_edge=True,
                )
            ]
        return result
    if tag == 2:  # HTML document fallback for non-HTML clients
        return parse_html_document(fields[0], inherited)
    if tag == 3:  # Image
        alt = fields[1] or "[图片]"
        return [DisplaySegment(text=alt, style=inherited.style if inherited else SegmentStyle())]
    if tag == 4:  # Shape
        return project_direct_shape(fields[0], inherited or DisplaySegment(""))
    if tag == 5:  # PRINTC-family semantic cell
        content, alignment, preferred_columns = fields
        nested: list[DisplaySegment] = []
        for child in content:
            nested.extend(parse_run(child, inherited))
        width = sum(_segment_columns(part) for part in nested)
        padding = max(0, preferred_columns - width)
        if padding:
            edge = nested[0 if alignment == 1 else -1] if nested else None
            if edge is None:
                nested = [DisplaySegment(" " * padding, logical_columns=padding)]
            elif alignment == 1:
                nested[0] = replace(
                    edge,
                    text=" " * padding + edge.text,
                    logical_columns=_segment_columns(edge) + padding,
                )
            else:
                nested[-1] = replace(
                    edge,
                    text=edge.text + " " * padding,
                    logical_columns=_segment_columns(edge) + padding,
                )
        return nested
    if tag == 6:  # Width-independent separator
        pattern = fields[0] or "-"
        return [DisplaySegment(pattern * max(1, DEFAULT_VIEWPORT_COLUMNS // len(pattern)))]
    if tag == 7:  # Semantic space
        width_tag, width_fields = unwrap_variant(fields[0])
        raw = width_fields[0]
        if isinstance(raw, list):
            raw = raw[0]
        columns = max(1, round(raw / (1000 if width_tag == 0 else 100)))
        return [DisplaySegment(" " * columns)]
    return [DisplaySegment(f"[未支持的显示片段 {tag}]")]


def _run_has_system_text_key(run: list[Any], key: int) -> bool:
    tag, fields = unwrap_variant(run)
    if tag in (0, 8):
        reference = fields[2] if len(fields) > 2 else None
        return isinstance(reference, dict) and reference.get(0) == key
    if tag in (1, 5):
        return any(_run_has_system_text_key(child, key) for child in fields[0])
    return False


def parse_html_document(
    document: dict[int, Any], inherited: DisplaySegment | None = None
) -> list[DisplaySegment]:
    """Project the normalized Emuera HTML tree into terminal display spans."""

    base = inherited or DisplaySegment("")
    nodes = document.get(0, [])
    if any(has_relative_division(node) for node in nodes):
        return project_positioned_html(
            nodes,
            base,
            font_context=_html_font_context,
            button_context=_html_button_context,
        )
    result: list[DisplaySegment] = []
    for node in nodes:
        result.extend(_parse_html_node(node, base))
    return result


def _parse_html_node(node: list[Any], context: DisplaySegment) -> list[DisplaySegment]:
    tag, fields = unwrap_variant(node)
    if tag == 0:  # Text
        return _html_text_segments(str(fields[0]), context)
    if tag != 1:
        return []

    kind = fields[0]
    children = fields[2]
    interaction = fields[3] if len(fields) > 3 else None
    semantic = fields[6] if len(fields) > 6 else None

    if kind == 13:  # br
        return [replace(context, text="\n", token=None, alignment=context.alignment)]
    if kind == 10:  # img
        return []
    if kind == 11:  # shape
        return project_html_shape(semantic, context)

    nested = context
    if kind == 0:  # b
        nested = replace(nested, style=replace(nested.style, bold=True))
    elif kind == 1:  # i
        nested = replace(nested, style=replace(nested.style, italic=True))
    elif kind == 2:  # u
        nested = replace(nested, style=replace(nested.style, underline=True))
    elif kind == 3:  # s
        nested = replace(nested, style=replace(nested.style, strike=True))
    elif kind == 4:  # font
        nested = _html_font_context(semantic, nested)
    elif kind == 5:  # p
        alignment = _semantic_field(semantic, 0, 0)
        nested = replace(nested, alignment=int(alignment))
    elif kind == 7:  # button
        nested = _html_button_context(semantic, interaction, nested)
    elif kind in (8, 9):  # nonbutton / clearbutton
        nested = replace(
            nested,
            token=None,
            enabled=True,
            title=None,
            hover_style=None,
            generation=None,
        )

    result: list[DisplaySegment] = []
    for child in children:
        result.extend(_parse_html_node(child, nested))
    return result


def _html_text_segments(text: str, context: DisplaySegment) -> list[DisplaySegment]:
    """Preserve the full Era cell occupied by box drawing inside tagged HTML."""

    if not text:
        return [replace(context, text="")]
    segments: list[DisplaySegment] = []
    plain_start = 0
    for index, character in enumerate(text):
        if not "\u2500" <= character <= "\u257f":
            continue
        if plain_start < index:
            segments.append(replace(context, text=text[plain_start:index]))
        segments.append(replace(context, text=character, logical_columns=2))
        plain_start = index + 1
    if plain_start < len(text):
        segments.append(replace(context, text=text[plain_start:]))
    return segments


def _html_font_context(semantic: Any, context: DisplaySegment) -> DisplaySegment:
    foreground = _semantic_field(semantic, 1)
    button_color = _semantic_field(semantic, 2)
    style = context.style
    hover_style = context.hover_style
    if isinstance(foreground, int):
        style = replace(style, foreground=_packed_color_hex(foreground))
    if isinstance(button_color, int):
        hover_style = replace(style, foreground=_packed_color_hex(button_color))
    return replace(context, style=style, hover_style=hover_style)


def _html_button_context(
    semantic: Any, interaction: dict[int, Any] | None, context: DisplaySegment
) -> DisplaySegment:
    if not interaction:
        return context
    title = _semantic_field(semantic, 1)
    return replace(
        context,
        token={0: int(interaction[0]), 1: int(interaction[1])},
        enabled=bool(interaction.get(5, True)),
        title=title if isinstance(title, str) else None,
        generation=int(interaction.get(4, 0)),
    )
