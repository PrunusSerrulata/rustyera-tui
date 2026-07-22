"""Portable projection of the runtime's normalized presentation state."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from rich.cells import cell_len

from .wire import unwrap_variant, variant

DEFAULT_VIEWPORT_COLUMNS = 100
SAVE_DELETE_PATTERN = re.compile(r"Delete save\d+\.sav")


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


@dataclass(frozen=True, slots=True)
class DisplayLineModel:
    line_id: int
    temporary: bool
    logical_line_start: bool
    line_end: bool
    alignment: int
    segments: tuple[DisplaySegment, ...]


@dataclass(slots=True)
class PresentationModel:
    revision: int = 0
    title: str = "RustyEra"
    lines: list[DisplayLineModel] = field(default_factory=list)
    input_wait: dict[int, Any] | None = None
    background: str = "#000000"
    button_focus: str = "#000000"
    _line_indices: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def apply_snapshot(self, snapshot: dict[int, Any]) -> None:
        self.revision = snapshot[0]
        self.title = snapshot[1]
        history = snapshot[2]
        self.lines = [parse_line(line) for line in history.get(0, [])]
        self._rebuild_line_indices()
        self.input_wait = snapshot.get(5)
        self._apply_settings(snapshot.get(6))

    def apply_delta(self, delta: dict[int, Any]) -> None:
        if delta[0] != self.revision:
            raise ValueError(
                f"presentation delta starts at {delta[0]}, but local revision is {self.revision}"
            )
        for operation in delta[2]:
            tag, fields = unwrap_variant(operation)
            if tag == 0:
                line = parse_line(fields[0])
                self._line_indices[line.line_id] = len(self.lines)
                self.lines.append(line)
            elif tag == 1:
                count = min(fields[0], len(self.lines))
                if count:
                    for line in self.lines[-count:]:
                        self._line_indices.pop(line.line_id, None)
                    del self.lines[-count:]
            elif tag == 2:
                self.lines.clear()
                self._line_indices.clear()
            elif tag == 3:
                self.title = fields[0]
            elif tag == 6:
                # minicbor omits an enum tuple field when Option is None. Consequently,
                # SetInputWait(None) is encoded as a zero-field variant, not `[None]`.
                self.input_wait = fields[0] if fields else None
            elif tag == 7:
                line_id, replacement = fields
                parsed = parse_line(replacement)
                index = self._line_indices.get(line_id)
                if index is not None:
                    self.lines[index] = parsed
                    if parsed.line_id != line_id:
                        self._line_indices.pop(line_id, None)
                        self._line_indices[parsed.line_id] = index
            elif tag == 8:
                self._apply_settings(fields[0])
            elif tag == 13:
                self._disable_old_buttons(fields[0])
            # Background images, audio, resource replay, HTML islands, redraw state, and
            # tooltip settings have no standalone terminal rendering operation.
        self.revision = delta[1]

    def _rebuild_line_indices(self) -> None:
        # Line IDs are runtime-issued stable identities. Indexing them avoids an O(history)
        # scan for every update to the pending logical line during output-heavy game startup.
        self._line_indices = {line.line_id: index for index, line in enumerate(self.lines)}

    def _apply_settings(self, settings: dict[int, Any] | None) -> None:
        if not settings:
            return
        self.background = color_hex(settings[2])
        self.button_focus = color_hex(settings[3])

    def _disable_old_buttons(self, generation: int) -> None:
        for index, line in enumerate(self.lines):
            segments = tuple(
                replace(segment, enabled=False)
                if segment.generation is not None and segment.generation != generation
                else segment
                for segment in line.segments
            )
            if segments != line.segments:
                self.lines[index] = replace(line, segments=segments)

    def has_enabled_button(self, token: dict[int, int]) -> bool:
        """Return whether the current projection still exposes an activatable token."""

        return any(
            segment.enabled and segment.token == token
            for line in self.lines
            for segment in line.segments
        )


@dataclass(slots=True)
class ServicePresentationModel:
    """Raw worker-side history used only to answer frontend service requests.

    Both rich and plain conversion are deferred until they are actually needed. Retaining
    decoded line values makes high-frequency PRINT updates cheap on the C ABI worker thread.
    """

    revision: int = 0
    lines: list[dict[int, Any]] = field(default_factory=list)
    input_wait: dict[int, Any] | None = None
    _line_indices: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def apply_snapshot(self, snapshot: dict[int, Any]) -> None:
        self.revision = snapshot[0]
        raw_lines = snapshot[2].get(0, [])
        self.lines = list(raw_lines)
        self._line_indices = {line[0]: index for index, line in enumerate(raw_lines)}
        self.input_wait = snapshot.get(5)

    def apply_delta(self, delta: dict[int, Any]) -> None:
        if delta[0] != self.revision:
            raise ValueError(
                f"presentation delta starts at {delta[0]}, but local revision is {self.revision}"
            )
        for operation in delta[2]:
            tag, fields = unwrap_variant(operation)
            if tag == 0:
                raw_line = fields[0]
                self._line_indices[raw_line[0]] = len(self.lines)
                self.lines.append(raw_line)
            elif tag == 1:
                count = min(fields[0], len(self.lines))
                if count:
                    first_removed = len(self.lines) - count
                    self._line_indices = {
                        line_id: index
                        for line_id, index in self._line_indices.items()
                        if index < first_removed
                    }
                    del self.lines[-count:]
            elif tag == 2:
                self.lines.clear()
                self._line_indices.clear()
            elif tag == 6:
                self.input_wait = fields[0] if fields else None
            elif tag == 7:
                line_id, raw_line = fields
                index = self._line_indices.get(line_id)
                if index is not None:
                    self.lines[index] = raw_line
                    if raw_line[0] != line_id:
                        self._line_indices.pop(line_id, None)
                        self._line_indices[raw_line[0]] = index
        self.revision = delta[1]


def plain_line(line: dict[int, Any]) -> str:
    return "".join(plain_run(run) for run in line.get(5, []))


def plain_run(run: list[Any]) -> str:
    tag, fields = unwrap_variant(run)
    if tag == 0:
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
        padding = " " * max(0, preferred_columns - cell_len(text))
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


def coalesce_presentation_deltas(deltas: list[dict[int, Any]]) -> dict[int, Any]:
    """Merge a contiguous poll batch while discarding superseded state operations."""

    if not deltas:
        raise ValueError("at least one presentation delta is required")
    operations: list[list[Any]] = []
    appended_lines: dict[int, int] = {}
    replaced_lines: dict[int, int] = {}
    state_operations: dict[int, int] = {}
    expected_revision = deltas[0][0]
    for delta in deltas:
        if delta[0] != expected_revision:
            raise ValueError(
                f"presentation delta batch expected revision {expected_revision}, got {delta[0]}"
            )
        expected_revision = delta[1]
        for operation in delta[2]:
            tag, fields = unwrap_variant(operation)
            if tag == 0:
                line_id = fields[0][0]
                appended_lines[line_id] = len(operations)
                replaced_lines.pop(line_id, None)
                operations.append(operation)
            elif tag == 7:
                line_id = fields[0]
                if line_id in appended_lines:
                    operations[appended_lines[line_id]] = variant(0, fields[1])
                elif line_id in replaced_lines:
                    operations[replaced_lines[line_id]] = operation
                else:
                    replaced_lines[line_id] = len(operations)
                    operations.append(operation)
            elif tag in (3, 4, 5, 6, 8, 9, 10, 11, 12):
                previous = state_operations.get(tag)
                if previous is None:
                    state_operations[tag] = len(operations)
                    operations.append(operation)
                else:
                    operations[previous] = operation
            else:
                # Deletion and clear alter which line an ID refers to. Retain the operation
                # and start a fresh line-coalescing region rather than guessing its target.
                appended_lines.clear()
                replaced_lines.clear()
                operations.append(operation)
    return {0: deltas[0][0], 1: deltas[-1][1], 2: operations}


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
    for run in line.get(5, []):
        segments.extend(parse_run(run))
    return DisplayLineModel(
        line_id=line[0],
        temporary=line.get(1, False),
        logical_line_start=line.get(2, True),
        line_end=line.get(3, True),
        alignment=line.get(4, 0),
        segments=tuple(segments),
    )


def parse_run(run: list[Any], inherited: DisplaySegment | None = None) -> list[DisplaySegment]:
    tag, fields = unwrap_variant(run)
    if tag == 0:  # Text
        text, style = fields[0], parse_style(fields[1])
        return [
            DisplaySegment(
                text=text,
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
        plain = "".join(part.text for part in nested)
        padding = max(0, preferred_columns - cell_len(plain))
        if padding:
            edge = nested[0 if alignment == 1 else -1] if nested else None
            if edge is None:
                nested = [DisplaySegment(" " * padding)]
            elif alignment == 1:
                nested[0] = replace(edge, text=" " * padding + edge.text)
            else:
                nested[-1] = replace(edge, text=edge.text + " " * padding)
        return nested
    if tag == 6:  # Width-independent separator
        pattern = fields[0] or "-"
        return [
            DisplaySegment(pattern * max(1, DEFAULT_VIEWPORT_COLUMNS // len(pattern)))
        ]
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
    if tag == 0:
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
    return f"#{(value >> 16) & 0xff:02x}{(value >> 8) & 0xff:02x}{value & 0xff:02x}"
