"""Portable projection of the runtime's normalized presentation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .wire import unwrap_variant


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

    def apply_snapshot(self, snapshot: dict[int, Any]) -> None:
        self.revision = snapshot[0]
        self.title = snapshot[1]
        history = snapshot[2]
        self.lines = [parse_line(line) for line in history.get(0, [])]
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
                self.lines.append(parse_line(fields[0]))
            elif tag == 1:
                count = min(fields[0], len(self.lines))
                if count:
                    del self.lines[-count:]
            elif tag == 2:
                self.lines.clear()
            elif tag == 3:
                self.title = fields[0]
            elif tag == 6:
                self.input_wait = fields[0]
            elif tag == 7:
                line_id, replacement = fields
                parsed = parse_line(replacement)
                for index, line in enumerate(self.lines):
                    if line.line_id == line_id:
                        self.lines[index] = parsed
                        break
            elif tag == 8:
                self._apply_settings(fields[0])
            # Background images, audio, resource replay, HTML islands, redraw state, and
            # button generations have no standalone terminal rendering operation.
        self.revision = delta[1]

    def _apply_settings(self, settings: dict[int, Any] | None) -> None:
        if not settings:
            return
        self.background = color_hex(settings[2])
        self.button_focus = color_hex(settings[3])


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
