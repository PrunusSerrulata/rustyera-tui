"""Textual widgets for semantic Era presentation lines and dropdown menus."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.widgets import Static

from .presentation import (
    DEFAULT_BUTTON_FOCUS,
    DisplayLineModel,
    DisplaySegment,
    SegmentStyle,
)
from .game_line_layout import project_responsive_segments as _project_responsive_segments
from .game_line_layout import terminal_segment_text as _terminal_segment_text


@dataclass(frozen=True, slots=True)
class ClickRegion:
    row: int
    start: int
    end: int
    token: dict[int, int]
    enabled: bool
    title: str | None


def _rich_style(style: SegmentStyle, *, disabled: bool = False) -> Style:
    return Style(
        color=style.foreground,
        bgcolor=style.background,
        bold=style.bold,
        italic=style.italic,
        underline=style.underline,
        strike=style.strike,
        dim=disabled,
    )


class GameLine(Static):
    """One no-wrap semantic line with cell-accurate inline hit regions."""

    can_focus = True

    class Activated(Message):
        def __init__(self, token: dict[int, int], title: str | None) -> None:
            super().__init__()
            self.token = token
            self.title = title

    def __init__(self, line: DisplayLineModel) -> None:
        super().__init__("", markup=False, classes="game-line")
        self.line = line
        self.regions: list[ClickRegion] = []
        self.hovered_region: int | None = None
        self.interactions_enabled = True
        self.mouse_enabled = True
        self.replace_full_width_spaces = False
        self.button_focus = DEFAULT_BUTTON_FOCUS
        self.layout_width: int | None = None
        self._projected_width: int | None = None
        self._projected_segments: tuple[DisplaySegment, ...] = ()

    def on_mount(self) -> None:
        self._render_line()

    def set_line(self, line: DisplayLineModel) -> None:
        self.line = line
        self.hovered_region = None
        self._projected_width = None
        self._render_line()

    def enable_interactions(self) -> None:
        if not self.interactions_enabled:
            self.interactions_enabled = True
            self._render_line()

    def disable_interactions(self) -> None:
        """Immediately retire hit regions while an activation is in flight."""

        if self.interactions_enabled:
            self.interactions_enabled = False
            self.hovered_region = None
            self.tooltip = None
            self._render_line()

    def _render_line(self) -> None:
        render_width = max(1, self.layout_width or self.size.width)
        output = Text(no_wrap=True, overflow="ignore")
        self.regions = []
        rows: list[list[tuple[str, DisplaySegment]]] = [[]]
        alignments = [self.line.alignment]
        for segment in self._segments_for_width(render_width):
            text = _terminal_segment_text(segment)
            if self.replace_full_width_spaces:
                text = text.replace("　", "  ")
            parts = text.split("\n")
            for index, part in enumerate(parts):
                if segment.alignment is not None:
                    alignments[-1] = segment.alignment
                if part:
                    rows[-1].append((part, segment))
                if index + 1 < len(parts):
                    rows.append([])
                    alignments.append(
                        segment.alignment if segment.alignment is not None else self.line.alignment
                    )

        layouts: list[list[tuple[str, DisplaySegment, int | None]]] = []
        for row_index, (row, alignment) in enumerate(zip(rows, alignments, strict=True)):
            content_width = sum(cell_len(text) for text, _segment in row)
            available = max(0, render_width - content_width)
            alignment_padding = {0: 0, 1: available // 2, 2: available}.get(alignment, 0)
            cursor = alignment_padding
            layout: list[tuple[str, DisplaySegment, int | None]] = []
            for text, segment in row:
                width = cell_len(text)
                if segment.right_edge:
                    gap = max(0, render_width - cursor - width)
                    if gap:
                        layout.append((" " * gap, DisplaySegment(" " * gap), None))
                        cursor += gap
                region_index = None
                if segment.token is not None:
                    if (
                        self.regions
                        and self.regions[-1].row == row_index
                        and self.regions[-1].end == cursor
                        and self.regions[-1].token == segment.token
                        and self.regions[-1].enabled == segment.enabled
                        and self.regions[-1].title == segment.title
                    ):
                        previous = self.regions[-1]
                        self.regions[-1] = ClickRegion(
                            previous.row,
                            previous.start,
                            cursor + width,
                            previous.token,
                            previous.enabled,
                            previous.title,
                        )
                        region_index = len(self.regions) - 1
                    else:
                        self.regions.append(
                            ClickRegion(
                                row_index,
                                cursor,
                                cursor + width,
                                segment.token,
                                segment.enabled,
                                segment.title,
                            )
                        )
                        region_index = len(self.regions) - 1
                layout.append((text, segment, region_index))
                cursor += width
            layouts.append(layout)

        for row_index, (layout, alignment) in enumerate(zip(layouts, alignments, strict=True)):
            content_width = sum(cell_len(text) for text, _segment, _region in layout)
            available = max(0, render_width - content_width)
            alignment_padding = {0: 0, 1: available // 2, 2: available}.get(alignment, 0)
            if alignment_padding:
                output.append(" " * alignment_padding)
            for text, segment, region_index in layout:
                hovered = region_index is not None and self.hovered_region == region_index
                selected_style = segment.style
                if hovered:
                    selected_style = segment.hover_style or replace(
                        segment.style,
                        foreground=self.button_focus,
                    )
                output.append(
                    text,
                    _rich_style(
                        selected_style,
                        disabled=segment.token is not None
                        and (not segment.enabled or not self.interactions_enabled),
                    ),
                )
            if row_index + 1 < len(layouts):
                output.append("\n")
        self.update(output)

    def set_layout_width(self, width: int) -> None:
        width = max(1, width)
        if self.layout_width != width:
            self.layout_width = width
            if self._is_width_sensitive() and self.is_mounted:
                self._render_line()

    def set_button_focus(self, color: str) -> None:
        if self.button_focus != color:
            self.button_focus = color
            if self.is_mounted:
                self._render_line()

    def set_replace_full_width_spaces(self, enabled: bool) -> None:
        if self.replace_full_width_spaces != enabled:
            self.replace_full_width_spaces = enabled
            if self.is_mounted:
                self._render_line()

    def on_resize(self, event: events.Resize) -> None:
        if (
            self.layout_width is None
            and self._is_width_sensitive()
            and event.size.width != self._projected_width
        ):
            self._render_line()

    def _is_width_sensitive(self) -> bool:
        return bool(
            self.line.layout
            or self.line.alignment in (1, 2)
            or any(segment.right_edge for segment in self.line.segments)
        )

    def _segments_for_width(self, width: int) -> tuple[DisplaySegment, ...]:
        if self._projected_width == width:
            return self._projected_segments
        self._projected_segments = _project_responsive_segments(self.line, max(1, width))
        self._projected_width = width
        return self._projected_segments

    def _region_at(self, x: int, y: int) -> int | None:
        for index, region in enumerate(self.regions):
            if region.row == y and region.start <= x < region.end:
                return index
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self.mouse_enabled:
            self.tooltip = None
            return
        index = self._region_at(event.offset.x, event.offset.y)
        if index is not None and (not self.interactions_enabled or not self.regions[index].enabled):
            index = None
        if index != self.hovered_region:
            self.hovered_region = index
            self._render_line()
        if index is not None and self.regions[index].title:
            self.tooltip = self.regions[index].title
        else:
            self.tooltip = None

    def on_leave(self, _event: events.Leave) -> None:
        if self.hovered_region is not None:
            self.hovered_region = None
            self._render_line()
        self.tooltip = None

    def on_click(self, event: events.Click) -> None:
        if not self.mouse_enabled or event.button != 1:
            return
        index = self._region_at(event.offset.x, event.offset.y)
        if index is None:
            return
        region = self.regions[index]
        event.stop()
        if self.interactions_enabled and region.enabled:
            self.post_message(self.Activated(region.token, region.title))


class GameViewport(ScrollableContainer):
    """Incrementally reconcile presentation lines while retaining scroll position."""

    class ContinueRequested(Message):
        """A non-button viewport click may satisfy an Enter or AnyKey wait."""

    class SkipMessageRequested(Message):
        """A secondary click requests continuous skipping for a message wait."""

    class HorizontalOverflowChanged(Message):
        def __init__(self, visible: bool) -> None:
            super().__init__()
            self.visible = visible

    def __init__(self) -> None:
        super().__init__(id="game-viewport")
        self.models: list[DisplayLineModel] = []
        self.interactions_enabled = True
        self.mouse_enabled = True
        self.replace_full_width_spaces = False
        self.presentation_background = "#000000"
        self.button_focus = DEFAULT_BUTTON_FOCUS
        self._horizontal_overflow = False
        self._projected_width = 0
        self._projected_line_widths: list[int] = []
        self._overflowing_line_count = 0
        self._source_line_count = 0
        self._source_prefix_lengths = [0]

    @property
    def content_width(self) -> int:
        """Width available to game lines after stable scrollbar gutters."""

        return max(1, self.scrollable_content_region.width)

    @property
    def content_dimensions(self) -> tuple[int, int]:
        region = self.scrollable_content_region
        return max(1, region.width), max(1, region.height)

    def on_resize(self, _event: events.Resize) -> None:
        # Plain left-aligned history is already no-wrap and needs no new Rich text.
        width = self.content_width
        for child in self.children:
            if isinstance(child, GameLine) and child._is_width_sensitive():
                child.set_layout_width(width)
        self._reproject_all_lines(width)
        self._set_horizontal_overflow(self._overflowing_line_count > 0)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if not self.mouse_enabled:
            return
        if event.button == 1:
            self.post_message(self.ContinueRequested())
        elif event.button == 3:
            self.post_message(self.SkipMessageRequested())

    def disable_interactions(self) -> None:
        self.interactions_enabled = False
        for child in self.children:
            if isinstance(child, GameLine):
                child.disable_interactions()

    def enable_interactions(self) -> None:
        self.interactions_enabled = True
        for child in self.children:
            if isinstance(child, GameLine):
                child.enable_interactions()

    def set_mouse_enabled(self, enabled: bool) -> None:
        self.mouse_enabled = enabled
        for child in self.children:
            if isinstance(child, GameLine):
                child.mouse_enabled = enabled

    def set_replace_full_width_spaces(self, enabled: bool) -> None:
        self.replace_full_width_spaces = enabled
        for child in self.children:
            if isinstance(child, GameLine):
                child.set_replace_full_width_spaces(enabled)

    def _line_widget(self, line: DisplayLineModel) -> GameLine:
        widget = GameLine(line)
        widget.layout_width = self.content_width
        widget.interactions_enabled = self.interactions_enabled
        widget.mouse_enabled = self.mouse_enabled
        widget.replace_full_width_spaces = self.replace_full_width_spaces
        widget.button_focus = self.button_focus
        widget.styles.background = self.presentation_background
        return widget

    def _set_horizontal_overflow(self, visible: bool) -> None:
        if visible != self._horizontal_overflow:
            self._horizontal_overflow = visible
            self.post_message(self.HorizontalOverflowChanged(visible))

    def _reproject_all_lines(self, width: int) -> None:
        self._projected_width = width
        self._projected_line_widths = [
            _projected_line_width(line, width) for line in self.models
        ]
        self._overflowing_line_count = sum(
            value > width for value in self._projected_line_widths
        )

    def set_presentation_background(self, color: str) -> None:
        if color == self.presentation_background:
            return
        self.presentation_background = color
        self.styles.background = color
        for child in self.children:
            if isinstance(child, GameLine):
                child.styles.background = color

    def set_button_focus(self, color: str) -> None:
        if color == self.button_focus:
            return
        self.button_focus = color
        for child in self.children:
            if isinstance(child, GameLine):
                child.set_button_focus(color)

    async def set_lines(
        self,
        lines: list[DisplayLineModel],
        changed_from: int | None = None,
        trimmed_prefix: int = 0,
    ) -> bool:
        source_lines = lines
        changed_from = None if changed_from is None else max(0, changed_from)
        changed_lines = source_lines if changed_from is None else source_lines[changed_from:]
        has_changed_right_edge = any(
            segment.right_edge for line in changed_lines for segment in line.segments
        )
        old = self.models
        children = list(self.children)
        incremental_suffix = (
            changed_from is not None
            and changed_from <= self._source_line_count
            and not has_changed_right_edge
            and not trimmed_prefix
        )
        if incremental_suffix:
            common = min(self._source_prefix_lengths[changed_from], len(old))
            tail = changed_lines
            prefix_lengths = self._source_prefix_lengths[: changed_from + 1]
            prefix_lengths.extend(common + offset for offset in range(1, len(tail) + 1))
        else:
            lines, prefix_lengths = _merge_save_delete_lines_with_prefixes(source_lines)
            common = 0
            for left, right in zip(old, lines, strict=False):
                if left != right:
                    break
                common += 1
            tail = lines[common:]
        next_length = common + len(tail)
        history_grew = next_length > len(old)
        if common == min(len(old), next_length):
            if next_length < len(old):
                await self.remove_children(children[next_length:])
            elif next_length > len(old):
                await self.mount(*(self._line_widget(line) for line in tail[len(old) - common :]))
        elif len(old) == next_length and len(children) == next_length:
            for index in range(common, next_length):
                child = children[index]
                replacement = tail[index - common]
                if isinstance(child, GameLine) and old[index] != replacement:
                    child.set_line(replacement)
        else:
            rebuilt = [*old[:common], *tail]
            await self.remove_children()
            await self.mount(*(self._line_widget(line) for line in rebuilt))
        self.models[common:] = tail
        self._source_line_count = len(source_lines)
        self._source_prefix_lengths = prefix_lengths
        width = self.content_width
        if width != self._projected_width:
            self._reproject_all_lines(width)
        else:
            self._overflowing_line_count -= sum(
                value > width for value in self._projected_line_widths[common:]
            )
            del self._projected_line_widths[common:]
            projected_tail = [
                _projected_line_width(line, width) for line in self.models[common:]
            ]
            self._projected_line_widths.extend(projected_tail)
            self._overflowing_line_count += sum(value > width for value in projected_tail)
        self._set_horizontal_overflow(self._overflowing_line_count > 0)
        # Reference Emuera leaves the scrollbar unchanged when CLEARLINE removes a dynamic
        # frame and the replacement restores the same line count. Follow genuinely appended
        # history, but let equal-length tail replacement update the existing rows in place.
        if history_grew:
            self.anchor()
        return self._horizontal_overflow


def _projected_line_width(line: DisplayLineModel, width: int) -> int:
    """Return the widest projected row without depending on a provisional layout."""

    widest = 0
    current = 0
    for segment in _project_responsive_segments(line, width):
        parts = _terminal_segment_text(segment).split("\n")
        for index, part in enumerate(parts):
            current += cell_len(part)
            if index + 1 < len(parts):
                widest = max(widest, current)
                current = 0
    return max(widest, current)


def _merge_save_delete_lines(lines: list[DisplayLineModel]) -> list[DisplayLineModel]:
    """Place a runtime save-slot delete action at the right edge of its slot row."""

    return _merge_save_delete_lines_with_prefixes(lines)[0]


def _merge_save_delete_lines_with_prefixes(
    lines: list[DisplayLineModel],
) -> tuple[list[DisplayLineModel], list[int]]:
    """Merge save actions and map each source prefix to its projected line count."""

    result: list[DisplayLineModel] = []
    prefix_lengths = [0]
    for line in lines:
        if len(line.segments) == 1 and line.segments[0].right_edge and result:
            previous = result[-1]
            result[-1] = replace(
                previous,
                segments=(*previous.segments, line.segments[0]),
            )
        else:
            result.append(line)
        prefix_lengths.append(len(result))
    return result, prefix_lengths
