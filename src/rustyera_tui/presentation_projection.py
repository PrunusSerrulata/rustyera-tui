"""Projection of normalized runtime runs into terminal display spans and service text."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from rich.cells import cell_len

from .presentation_types import (
    DEFAULT_VIEWPORT_COLUMNS,
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    SegmentStyle,
    SeparatorLayout,
)
from .wire import unwrap_variant

SAVE_DELETE_PATTERN = re.compile(r"Delete save\d+\.sav")


def plain_line(line: dict[int, Any]) -> str:
    return "".join(plain_run(run) for run in line.get(5, []))


def html_printed_str(lines: list[dict[int, Any]], line_number: int) -> str:
    """Serialize one newest-first logical line using Emuera's HTML wrapper."""

    if line_number < 0:
        return ""
    count = 0
    selected: list[dict[int, Any]] = []
    for line in reversed(lines):
        if count == line_number:
            selected.insert(0, line)
        if line.get(2, True):
            count += 1
        if count > line_number:
            break
    if not selected:
        return ""
    alignment = {0: "left", 1: "center", 2: "right"}.get(selected[0].get(4, 0), "left")
    body = "<br>".join(_escape_html(plain_line(line)) for line in selected)
    return f"<p align='{alignment}'><nobr>{body}</nobr></p>"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace(">", "&gt;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def plain_run(run: list[Any]) -> str:
    tag, fields = unwrap_variant(run)
    if tag in (0, 8):
        return fields[0]
    if tag == 1:
        return "".join(plain_run(child) for child in fields[0])
    if tag == 2:
        return "".join(segment.text for segment in parse_html_document(fields[0]))
    if tag == 3:
        return fields[1] or "[图片]"
    if tag == 4:
        return "[图形]"
    if tag == 5:
        content, alignment, preferred_columns = fields
        text = "".join(plain_run(child) for child in content)
        width = sum(_plain_run_columns(child) for child in content)
        padding = " " * max(0, preferred_columns - width)
        return f"{padding}{text}" if alignment == 1 else f"{text}{padding}"
    if tag == 6:
        pattern = fields[0] or "-"
        return pattern * max(1, DEFAULT_VIEWPORT_COLUMNS // len(pattern))
    if tag == 7:
        width_tag, width_fields = unwrap_variant(fields[0])
        raw = width_fields[0]
        if isinstance(raw, list):
            raw = raw[0]
        columns = max(1, round(raw / (1000 if width_tag == 0 else 100)))
        return " " * columns
    return f"[未支持的显示片段 {tag}]"


def _plain_run_columns(run: list[Any]) -> int:
    tag, fields = unwrap_variant(run)
    if tag == 8:
        return max(0, int(fields[3]))
    if tag == 0:
        return cell_len(fields[0])
    if tag == 1:
        return sum(_plain_run_columns(child) for child in fields[0])
    if tag == 5:
        return sum(_plain_run_columns(child) for child in fields[0])
    return cell_len(plain_run(run))


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
        return [
            DisplaySegment(text="[图形]", style=inherited.style if inherited else SegmentStyle())
        ]
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


def _segment_columns(segment: DisplaySegment) -> int:
    if segment.logical_columns is not None and "\n" not in segment.text:
        return segment.logical_columns
    return cell_len(segment.text)


def parse_html_document(
    document: dict[int, Any], inherited: DisplaySegment | None = None
) -> list[DisplaySegment]:
    """Project the normalized Emuera HTML tree into terminal display spans."""

    base = inherited or DisplaySegment("")
    result: list[DisplaySegment] = []
    for node in document.get(0, []):
        result.extend(_parse_html_node(node, base))
    return result


def _parse_html_node(node: list[Any], context: DisplaySegment) -> list[DisplaySegment]:
    tag, fields = unwrap_variant(node)
    if tag == 0:  # Text
        return [replace(context, text=fields[0])]
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
        return [_html_shape_segment(semantic, context)]

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


def _html_shape_segment(semantic: Any, context: DisplaySegment) -> DisplaySegment:
    kind = _semantic_field(semantic, 0, "")
    parameters = _semantic_field(semantic, 1, [])
    if kind == "space" and parameters:
        width_tag, width_fields = unwrap_variant(parameters[0])
        raw = width_fields[0]
        if isinstance(raw, list):
            raw = raw[0]
        # A unitless HTML space uses hundredths of font height. eraTW's helper
        # deliberately emits 50 per requested half-width terminal cell.
        columns = max(1, round(raw / (1 if width_tag == 0 else 50)))
        return replace(context, text=" " * columns)
    return replace(context, text="[图形]")


def _semantic_field(semantic: Any, index: int, default: Any = None) -> Any:
    if semantic is None:
        return default
    try:
        _tag, fields = unwrap_variant(semantic)
    except ValueError:
        return default
    return fields[index] if index < len(fields) else default


def _packed_color_hex(value: int) -> str:
    return f"#{(value >> 16) & 0xFF:02x}{(value >> 8) & 0xFF:02x}{value & 0xFF:02x}"
