"""Textual widgets for semantic Era presentation lines and dropdown menus."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rich.cells import cell_len, set_cell_size
from rich.style import Style
from rich.text import Text
from textual import events
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.widgets import Static

from .presentation import (
    MAX_TABLE_COLUMN_WIDTH,
    MIN_TABLE_COLUMN_WIDTH,
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    SegmentStyle,
    SeparatorLayout,
)


@dataclass(frozen=True, slots=True)
class ClickRegion:
    row: int
    start: int
    end: int
    token: dict[int, int]
    enabled: bool
    title: str | None


def _rich_style(style: SegmentStyle, *, disabled: bool = False, hovered: bool = False) -> Style:
    return Style(
        color=style.foreground,
        bgcolor=style.background,
        bold=style.bold,
        italic=style.italic,
        underline=style.underline or hovered,
        strike=style.strike,
        dim=disabled,
        reverse=hovered,
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
            parts = segment.text.split("\n")
            for index, part in enumerate(parts):
                if segment.alignment is not None:
                    alignments[-1] = segment.alignment
                if part:
                    rows[-1].append((part, segment))
                if index + 1 < len(parts):
                    rows.append([])
                    alignments.append(
                        segment.alignment
                        if segment.alignment is not None
                        else self.line.alignment
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

        for row_index, (layout, alignment) in enumerate(
            zip(layouts, alignments, strict=True)
        ):
            content_width = sum(cell_len(text) for text, _segment, _region in layout)
            available = max(0, render_width - content_width)
            alignment_padding = {0: 0, 1: available // 2, 2: available}.get(alignment, 0)
            if alignment_padding:
                output.append(" " * alignment_padding)
            for text, segment, region_index in layout:
                hovered = region_index is not None and self.hovered_region == region_index
                selected_style = (
                    segment.hover_style if hovered and segment.hover_style else segment.style
                )
                output.append(
                    text,
                    _rich_style(
                        selected_style,
                        disabled=segment.token is not None
                        and (not segment.enabled or not self.interactions_enabled),
                        hovered=hovered,
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
        index = self._region_at(event.offset.x, event.offset.y)
        if index is not None and (
            not self.interactions_enabled or not self.regions[index].enabled
        ):
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
        if event.button != 1:
            return
        index = self._region_at(event.offset.x, event.offset.y)
        if index is None:
            return
        region = self.regions[index]
        event.stop()
        if self.interactions_enabled and region.enabled:
            self.post_message(self.Activated(region.token, region.title))


def _project_responsive_segments(
    line: DisplayLineModel, width: int
) -> tuple[DisplaySegment, ...]:
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
            projected.append(DisplaySegment(_separator_text(item.pattern, width)))
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
        projected.extend(_project_column_group(line.segments, cells, width, projected))
        segment_index = cells[-1].end

    projected.extend(line.segments[segment_index:])
    return tuple(projected)


def _project_column_group(
    segments: tuple[DisplaySegment, ...],
    cells: list[ColumnCellLayout],
    width: int,
    preceding: list[DisplaySegment],
) -> list[DisplaySegment]:
    # Keep every source cell on one physical row until its slot would fall below
    # the readable minimum. Spare width then grows all slots evenly up to the
    # script preference and the TUI cap, so resizing does not require reparsing.
    capacity = max(1, width // MIN_TABLE_COLUMN_WIDTH)
    row_columns = min(len(cells), capacity)
    preferred = max(
        MIN_TABLE_COLUMN_WIDTH,
        min(
            MAX_TABLE_COLUMN_WIDTH,
            max(cell.preferred_columns for cell in cells),
        ),
    )
    available_per_column = (
        width // row_columns if width >= MIN_TABLE_COLUMN_WIDTH else MIN_TABLE_COLUMN_WIDTH
    )
    column_width = max(
        MIN_TABLE_COLUMN_WIDTH,
        min(MAX_TABLE_COLUMN_WIDTH, preferred, available_per_column),
    )

    result: list[DisplaySegment] = []
    cursor = _last_row_width(preceding)
    cells_on_row = 0
    for cell in cells:
        if cells_on_row >= capacity or (
            cursor > 0 and cursor + MIN_TABLE_COLUMN_WIDTH > width
        ):
            result.append(DisplaySegment("\n"))
            cursor = 0
            cells_on_row = 0
        content = _pad_column_cell(
            segments[cell.start : cell.end],
            cell.alignment,
            column_width,
        )
        content_width = sum(cell_len(segment.text) for segment in content)
        result.extend(content)
        cursor += content_width
        cells_on_row += 1
    return result


def _pad_column_cell(
    content: tuple[DisplaySegment, ...],
    alignment: int,
    column_width: int,
) -> list[DisplaySegment]:
    result = list(content)
    content_width = sum(cell_len(segment.text) for segment in result)
    padding = max(0, column_width - content_width)
    if not padding:
        return result
    if not result:
        return [DisplaySegment(" " * padding)]
    edge_index = 0 if alignment == 1 else -1
    edge = result[edge_index]
    result[edge_index] = replace(
        edge,
        text=(" " * padding + edge.text)
        if alignment == 1
        else (edge.text + " " * padding),
    )
    return result


def _separator_text(pattern: str, width: int) -> str:
    pattern = pattern or "-"
    pattern_width = max(1, cell_len(pattern))
    repeated = pattern * (width // pattern_width + 1)
    return set_cell_size(repeated, width)


def _last_row_width(segments: list[DisplaySegment]) -> int:
    width = 0
    for segment in reversed(segments):
        if "\n" not in segment.text:
            width += cell_len(segment.text)
            continue
        width += cell_len(segment.text.rsplit("\n", 1)[-1])
        break
    return width


class GameViewport(ScrollableContainer):
    """Incrementally reconcile presentation lines while retaining scroll position."""

    class ContinueRequested(Message):
        """A non-button viewport click may satisfy a pure Enter wait."""

    class SkipEnterRequested(Message):
        """A secondary click requests continuous message-skip for Enter waits."""

    class HorizontalScrollbarChanged(Message):
        def __init__(self, visible: bool) -> None:
            super().__init__()
            self.visible = visible

    def __init__(self) -> None:
        super().__init__(id="game-viewport")
        self.models: list[DisplayLineModel] = []
        self.interactions_enabled = True
        self.presentation_background = "#000000"

    def on_resize(self, event: events.Resize) -> None:
        # Plain left-aligned history is already no-wrap and needs no new Rich text.
        for child in self.children:
            if isinstance(child, GameLine) and child._is_width_sensitive():
                child.set_layout_width(event.size.width)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if event.button == 1:
            self.post_message(self.ContinueRequested())
        elif event.button == 3:
            self.post_message(self.SkipEnterRequested())

    def watch_show_horizontal_scrollbar(self, visible: bool) -> None:
        self.post_message(self.HorizontalScrollbarChanged(visible))

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

    def _line_widget(self, line: DisplayLineModel) -> GameLine:
        widget = GameLine(line)
        widget.layout_width = max(1, self.size.width)
        widget.interactions_enabled = self.interactions_enabled
        widget.styles.background = self.presentation_background
        return widget

    def set_presentation_background(self, color: str) -> None:
        self.presentation_background = color
        self.styles.background = color
        for child in self.children:
            if isinstance(child, GameLine):
                child.styles.background = color

    async def set_lines(
        self,
        lines: list[DisplayLineModel],
        changed_from: int | None = None,
        trimmed_prefix: int = 0,
    ) -> None:
        lines = _merge_save_delete_lines(lines)
        old = self.models
        children = list(self.children)
        if trimmed_prefix and not any(
            segment.right_edge for line in lines for segment in line.segments
        ):
            count = min(trimmed_prefix, len(old), len(children))
            if count:
                await self.remove_children(children[:count])
                old = old[count:]
                children = children[count:]
        if changed_from is None or any(
            segment.right_edge for line in lines for segment in line.segments
        ):
            common = 0
            for left, right in zip(old, lines, strict=False):
                if left != right:
                    break
                common += 1
        else:
            common = min(changed_from, len(old), len(lines))
        if common == min(len(old), len(lines)):
            if len(lines) < len(old):
                await self.remove_children(children[len(lines) :])
            elif len(lines) > len(old):
                await self.mount(*(self._line_widget(line) for line in lines[len(old) :]))
        elif len(old) == len(lines) and len(children) == len(lines):
            for index in range(common, len(lines)):
                child = children[index]
                if isinstance(child, GameLine) and old[index] != lines[index]:
                    child.set_line(lines[index])
        else:
            await self.remove_children()
            await self.mount(*(self._line_widget(line) for line in lines))
        self.models = list(lines)
        # Re-anchor after every presentation change so the next layout pass follows
        # the newly appended content even when the user had scrolled into history.
        self.anchor()


def _merge_save_delete_lines(lines: list[DisplayLineModel]) -> list[DisplayLineModel]:
    """Place a runtime save-slot delete action at the right edge of its slot row."""

    result: list[DisplayLineModel] = []
    for line in lines:
        if len(line.segments) == 1 and line.segments[0].right_edge and result:
            previous = result[-1]
            result[-1] = replace(
                previous,
                segments=(*previous.segments, line.segments[0]),
            )
        else:
            result.append(line)
    return result
