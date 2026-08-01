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


@dataclass(slots=True)
class PresentationModel:
    revision: int = 0
    title: str = "RustyEra"
    lines: list[DisplayLineModel] = field(default_factory=list)
    input_wait: dict[int, Any] | None = None
    background: str = "#000000"
    button_focus: str = "#000000"
    maximum_physical_lines: int = VIEWPORT_BUFFER_LINES
    changed_from: int | None = 0
    trimmed_prefix: int = 0
    # Runtime TrimLines counts canonical rows that may already be outside this viewport.
    _hidden_prefix: int = field(default=0, init=False, repr=False)
    _line_indices: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def apply_snapshot(self, snapshot: dict[int, Any]) -> None:
        self.revision = snapshot[0]
        self.title = snapshot[1]
        history = snapshot[2]
        self.lines = [parse_line(line) for line in history.get(0, [])]
        self._hidden_prefix = 0
        self._trim_viewport_lines()
        self._rebuild_line_indices()
        self.changed_from = 0
        self.trimmed_prefix = 0
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
                self._mark_changed(len(self.lines))
                line = parse_line(fields[0])
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
                parsed = parse_line(replacement)
                index = self._line_indices.get(line_id)
                if index is not None:
                    self._mark_changed(index)
                    self.lines[index] = parsed
                    if parsed.line_id != line_id:
                        self._line_indices.pop(line_id, None)
                        self._line_indices[parsed.line_id] = index
            elif tag == 8:
                self._apply_settings(fields[0])
            elif tag == 13:
                self._disable_old_buttons(fields[0])
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
        count = len(self.lines) - VIEWPORT_BUFFER_LINES
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
        # The runtime setting describes its canonical physical history. The terminal
        # viewport deliberately keeps a smaller, frontend-owned logical-line buffer.
        self.maximum_physical_lines = VIEWPORT_BUFFER_LINES

    def _disable_old_buttons(self, generation: int) -> None:
        for index, line in enumerate(self.lines):
            segments = tuple(
                replace(segment, enabled=False)
                if segment.generation is not None and segment.generation != generation
                else segment
                for segment in line.segments
            )
            if segments != line.segments:
                self._mark_changed(index)
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
            elif tag == 14:
                count = min(fields[0], len(self.lines))
                if count:
                    del self.lines[:count]
                    self._line_indices = {line[0]: index for index, line in enumerate(self.lines)}
        self.revision = delta[1]


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
