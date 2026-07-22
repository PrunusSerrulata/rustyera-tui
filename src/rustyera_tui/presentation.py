"""Portable projection of the runtime's normalized presentation state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .wire import unwrap_variant, variant


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
        return "".join(_collect_strings(fields[0]))
    if tag == 3:
        return fields[1] or "[图片]"
    if tag == 4:
        return "[图形]"
    if tag == 5:
        content, alignment, preferred_columns = fields
        text = "".join(plain_run(child) for child in content)
        padding = " " * max(0, preferred_columns - len(text))
        return f"{padding}{text}" if alignment == 1 else f"{text}{padding}"
    if tag == 6:
        pattern = fields[0] or "-"
        return pattern * max(1, 120 // len(pattern))
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
        return result
    if tag == 2:  # HTML document fallback for non-HTML clients
        text = "".join(_collect_strings(fields[0]))
        return [DisplaySegment(text=text, style=inherited.style if inherited else SegmentStyle())]
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
        padding = max(0, preferred_columns - len(plain))
        if padding:
            space = DisplaySegment(" " * padding)
            nested = ([space] + nested) if alignment == 1 else (nested + [space])
        return nested
    if tag == 6:  # Width-independent separator
        pattern = fields[0] or "-"
        return [DisplaySegment(pattern * max(1, 120 // len(pattern)))]
    if tag == 7:  # Semantic space
        width_tag, width_fields = unwrap_variant(fields[0])
        raw = width_fields[0]
        if isinstance(raw, list):
            raw = raw[0]
        columns = max(1, round(raw / (1000 if width_tag == 0 else 100)))
        return [DisplaySegment(" " * columns)]
    return [DisplaySegment(f"[未支持的显示片段 {tag}]")]


def _collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_collect_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_collect_strings(item))
        return result
    return []
