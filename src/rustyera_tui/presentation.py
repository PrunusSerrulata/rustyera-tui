"""Portable projection of the runtime's normalized presentation state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .presentation_projection import (
    color_hex,
    html_printed_str as html_printed_str,
    parse_html_document as parse_html_document,
    parse_line as parse_line,
    parse_run as parse_run,
    parse_style as parse_style,
    plain_line as plain_line,
    plain_run as plain_run,
)
from .presentation_service import ServicePresentationModel as ServicePresentationModel
from .presentation_types import (
    DEFAULT_VIEWPORT_COLUMNS as DEFAULT_VIEWPORT_COLUMNS,
    MAX_TABLE_COLUMN_WIDTH as MAX_TABLE_COLUMN_WIDTH,
    MIN_TABLE_COLUMN_WIDTH as MIN_TABLE_COLUMN_WIDTH,
    TARGET_TABLE_COLUMNS as TARGET_TABLE_COLUMNS,
    ColumnCellLayout as ColumnCellLayout,
    DisplayLineModel as DisplayLineModel,
    DisplaySegment as DisplaySegment,
    SegmentStyle as SegmentStyle,
    SeparatorLayout as SeparatorLayout,
)
from .wire import unwrap_variant, variant

VIEWPORT_BUFFER_LINES = 1_000
DEFAULT_BUTTON_FOCUS = "#ffff00"
DEFAULT_PRESENTATION_TITLE = "RustyEra"


@dataclass(slots=True)
class PresentationModel:
    revision: int = 0
    title: str = DEFAULT_PRESENTATION_TITLE
    lines: list[DisplayLineModel] = field(default_factory=list)
    input_wait: dict[int, Any] | None = None
    background: str = "#000000"
    button_focus: str = DEFAULT_BUTTON_FOCUS
    maximum_physical_lines: int = VIEWPORT_BUFFER_LINES
    drawable_width: int = 760_000
    changed_from: int | None = 0
    trimmed_prefix: int = 0
    _button_generation: int | None = field(default=None, init=False, repr=False)
    _retired_button_tokens: set[tuple[int, int]] = field(
        default_factory=set, init=False, repr=False
    )
    # Runtime TrimLines counts canonical rows that may already be outside this viewport.
    _hidden_prefix: int = field(default=0, init=False, repr=False)
    _line_indices: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def apply_snapshot(self, snapshot: dict[int, Any]) -> None:
        self.revision = snapshot[0]
        self.title = snapshot[1]
        # Snapshots carry each button's authoritative enabled state, but not the
        # current BREAKBUTTON generation. Wait for a generation delta before
        # filtering later partial line updates locally.
        self._button_generation = None
        history = snapshot[2]
        self._hidden_prefix = 0
        self._apply_settings(snapshot.get(6))
        self.lines = [
            self._prepare_line(self._project_separator_width(parse_line(line)))
            for line in history.get(0, [])
        ]
        self._trim_viewport_lines()
        self._rebuild_line_indices()
        self.changed_from = 0
        self.trimmed_prefix = 0
        self.input_wait = snapshot.get(5)

    def apply_delta(self, delta: dict[int, Any]) -> None:
        if delta[0] != self.revision:
            raise ValueError(
                f"presentation delta starts at {delta[0]}, but local revision is {self.revision}"
            )
        for operation in delta[2]:
            tag, fields = unwrap_variant(operation)
            if tag == 0:
                self._mark_changed(len(self.lines))
                line = self._prepare_line(self._project_separator_width(parse_line(fields[0])))
                self._line_indices[line.line_id] = len(self.lines)
                self.lines.append(line)
            elif tag == 1:
                requested = fields[0]
                count = min(requested, len(self.lines))
                if count:
                    for line in self.lines[-count:]:
                        self._line_indices.pop(line.line_id, None)
                    del self.lines[-count:]
                    self._mark_changed(len(self.lines))
                self._hidden_prefix = max(0, self._hidden_prefix - (requested - count))
            elif tag == 2:
                self.lines.clear()
                self._hidden_prefix = 0
                self._line_indices.clear()
                self._mark_changed(0)
            elif tag == 3:
                self.title = fields[0]
            elif tag == 6:
                # minicbor omits an enum tuple field when Option is None. Consequently,
                # SetInputWait(None) is encoded as a zero-field variant, not `[None]`.
                self.input_wait = fields[0] if fields else None
            elif tag == 7:
                line_id, replacement = fields
                parsed = self._prepare_line(
                    self._project_separator_width(parse_line(replacement))
                )
                index = self._line_indices.get(line_id)
                if index is not None:
                    self._mark_changed(index)
                    self.lines[index] = parsed
                    if parsed.line_id != line_id:
                        self._line_indices.pop(line_id, None)
                        self._line_indices[parsed.line_id] = index
            elif tag == 8:
                previous_drawable_width = self.drawable_width
                self._apply_settings(fields[0])
                if self.drawable_width != previous_drawable_width:
                    self.lines = [self._project_separator_width(line) for line in self.lines]
                    self._mark_changed(0)
            elif tag == 13:
                self._button_generation = int(fields[0])
                self._disable_old_buttons(self._button_generation)
            elif tag == 14:
                requested = fields[0]
                hidden = min(requested, self._hidden_prefix)
                self._hidden_prefix -= hidden
                count = min(requested - hidden, len(self.lines))
                if count:
                    del self.lines[:count]
                    self._rebuild_line_indices()
                    self.trimmed_prefix += count
                    if self.changed_from is None:
                        self.changed_from = len(self.lines)
                    else:
                        self.changed_from = max(0, self.changed_from - count)
            # Background images, audio, resource replay, HTML islands, redraw state, and
            # tooltip settings have no standalone terminal rendering operation.
        self._trim_viewport_lines()
        self.revision = delta[1]

    def take_render_change(self) -> tuple[int | None, int]:
        changed_from = self.changed_from
        trimmed_prefix = self.trimmed_prefix
        self.changed_from = None
        self.trimmed_prefix = 0
        return changed_from, trimmed_prefix

    def _mark_changed(self, index: int) -> None:
        self.changed_from = index if self.changed_from is None else min(self.changed_from, index)

    def _rebuild_line_indices(self) -> None:
        # Line IDs are runtime-issued stable identities. Indexing them avoids an O(history)
        # scan for every update to the pending logical line during output-heavy game startup.
        self._line_indices = {line.line_id: index for index, line in enumerate(self.lines)}

    def _trim_viewport_lines(self) -> None:
        count = len(self.lines) - self.maximum_physical_lines
        if count <= 0:
            return
        del self.lines[:count]
        self._hidden_prefix += count
        self._rebuild_line_indices()
        self.trimmed_prefix += count
        if self.changed_from is not None:
            self.changed_from = max(0, self.changed_from - count)

    def _apply_settings(self, settings: dict[int, Any] | None) -> None:
        if not settings:
            return
        self.background = color_hex(settings[2])
        self.button_focus = color_hex(settings[3])
        self.maximum_physical_lines = max(500, int(settings.get(4, VIEWPORT_BUFFER_LINES)))
        self.drawable_width = max(1, int(settings.get(0, self.drawable_width)))
        self._trim_viewport_lines()

    def _project_separator_width(self, line: DisplayLineModel) -> DisplayLineModel:
        layout = tuple(
            replace(
                item,
                # Runtime dimensions are milli-pixels; one terminal half-width cell is
                # approximated as half the configured font size, hence the factor of two.
                maximum_columns=max(1, self.drawable_width * 2 // item.font_millipixels),
            )
            if isinstance(item, SeparatorLayout) and item.font_millipixels > 0
            else item
            for item in line.layout
        )
        return replace(line, layout=layout) if layout != line.layout else line

    def _disable_old_buttons(self, generation: int) -> None:
        for index, line in enumerate(self.lines):
            updated = self._line_for_button_generation(line, generation)
            if updated != line:
                self._mark_changed(index)
                self.lines[index] = updated

    def _line_for_current_button_generation(self, line: DisplayLineModel) -> DisplayLineModel:
        if self._button_generation is None:
            return line
        return self._line_for_button_generation(line, self._button_generation)

    def _prepare_line(self, line: DisplayLineModel) -> DisplayLineModel:
        line = self._line_for_current_button_generation(line)
        segments = tuple(
            replace(segment, enabled=False)
            if segment.enabled
            and self._token_identity(segment.token) in self._retired_button_tokens
            else segment
            for segment in line.segments
        )
        return replace(line, segments=segments) if segments != line.segments else line

    @staticmethod
    def _line_for_button_generation(line: DisplayLineModel, generation: int) -> DisplayLineModel:
        segments = tuple(
            replace(segment, enabled=False)
            if segment.enabled
            and segment.generation is not None
            and segment.generation != generation
            else segment
            for segment in line.segments
        )
        return replace(line, segments=segments) if segments != line.segments else line

    def retire_enabled_buttons(self) -> set[tuple[int, int]]:
        """Retire buttons visible when the frontend submits the active wait."""

        tokens = {
            identity
            for line in self.lines
            for segment in line.segments
            if segment.enabled and (identity := self._token_identity(segment.token)) is not None
        }
        if not tokens:
            return tokens
        self._retired_button_tokens.update(tokens)
        self._set_button_tokens_enabled(tokens, False)
        return tokens

    def restore_buttons(self, tokens: set[tuple[int, int]]) -> None:
        """Restore a retired submission after the runtime rejects that interaction."""

        if not tokens:
            return
        self._retired_button_tokens.difference_update(tokens)
        self._set_button_tokens_enabled(tokens, True)

    def _set_button_tokens_enabled(self, tokens: set[tuple[int, int]], enabled: bool) -> None:
        for index, line in enumerate(self.lines):
            segments = tuple(
                replace(segment, enabled=enabled)
                if self._token_identity(segment.token) in tokens
                and segment.enabled != enabled
                and (
                    not enabled
                    or self._button_generation is None
                    or segment.generation is None
                    or segment.generation == self._button_generation
                )
                else segment
                for segment in line.segments
            )
            if segments != line.segments:
                self._mark_changed(index)
                self.lines[index] = replace(line, segments=segments)

    @staticmethod
    def _token_identity(token: dict[int, int] | None) -> tuple[int, int] | None:
        if token is None:
            return None
        return int(token[0]), int(token[1])

    def has_enabled_button(self, token: dict[int, int]) -> bool:
        """Return whether the current projection still exposes an activatable token."""

        return any(
            segment.enabled and segment.token == token
            for line in self.lines
            for segment in line.segments
        )


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


ServicePresentationModel.__module__ = __name__
