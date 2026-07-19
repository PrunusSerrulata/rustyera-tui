"""Textual widgets for semantic Era presentation lines and dropdown menus."""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.widgets import Static

from .presentation import DisplayLineModel, SegmentStyle


@dataclass(frozen=True, slots=True)
class ClickRegion:
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
        def __init__(self, token: dict[int, int]) -> None:
            super().__init__()
            self.token = token

    def __init__(self, line: DisplayLineModel) -> None:
        super().__init__("", markup=False, classes="game-line")
        self.line = line
        self.regions: list[ClickRegion] = []
        self.hovered_region: int | None = None

    def on_mount(self) -> None:
        self._render_line()

    def set_line(self, line: DisplayLineModel) -> None:
        self.line = line
        self.hovered_region = None
        self._render_line()

    def _render_line(self) -> None:
        output = Text(no_wrap=True, overflow="ignore")
        self.regions = []
        content_width = sum(cell_len(segment.text) for segment in self.line.segments)
        available = max(0, self.size.width - content_width)
        alignment_padding = {0: 0, 1: available // 2, 2: available}.get(self.line.alignment, 0)
        if alignment_padding:
            output.append(" " * alignment_padding)
        cursor = alignment_padding
        for segment in self.line.segments:
            hovered = (
                segment.token is not None
                and self.hovered_region is not None
                and self.hovered_region == len(self.regions)
            )
            selected_style = (
                segment.hover_style if hovered and segment.hover_style else segment.style
            )
            output.append(
                segment.text,
                _rich_style(
                    selected_style,
                    disabled=segment.token is not None and not segment.enabled,
                    hovered=hovered,
                ),
            )
            width = cell_len(segment.text)
            if segment.token is not None:
                self.regions.append(
                    ClickRegion(
                        cursor, cursor + width, segment.token, segment.enabled, segment.title
                    )
                )
            cursor += width
        self.update(output)

    def on_resize(self, _event: events.Resize) -> None:
        self._render_line()

    def _region_at(self, x: int) -> int | None:
        for index, region in enumerate(self.regions):
            if region.start <= x < region.end:
                return index
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        index = self._region_at(event.offset.x)
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
        index = self._region_at(event.offset.x)
        if index is None:
            return
        region = self.regions[index]
        if region.enabled:
            event.stop()
            self.post_message(self.Activated(region.token))


class GameViewport(ScrollableContainer):
    """Incrementally reconcile presentation lines while retaining scroll position."""

    class ContinueRequested(Message):
        """A non-button viewport click may satisfy a pure Enter wait."""

    def __init__(self) -> None:
        super().__init__(id="game-viewport")
        self.models: list[DisplayLineModel] = []

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.ContinueRequested())

    async def set_lines(self, lines: list[DisplayLineModel]) -> None:
        was_at_end = self.is_vertical_scroll_end
        old = self.models
        children = list(self.children)
        common = 0
        for left, right in zip(old, lines, strict=False):
            if left != right:
                break
            common += 1
        if common == min(len(old), len(lines)):
            if len(lines) < len(old):
                await self.remove_children(children[len(lines) :])
            elif len(lines) > len(old):
                await self.mount(*(GameLine(line) for line in lines[len(old) :]))
        elif len(old) == len(lines) and len(children) == len(lines):
            for index in range(common, len(lines)):
                child = children[index]
                if isinstance(child, GameLine) and old[index] != lines[index]:
                    child.set_line(lines[index])
        else:
            await self.remove_children()
            await self.mount(*(GameLine(line) for line in lines))
        self.models = list(lines)
        if was_at_end or not old:
            self.call_after_refresh(self.scroll_end, animate=False, x_axis=False)
