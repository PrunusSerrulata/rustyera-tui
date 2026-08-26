"""Portable projection of the runtime's normalized presentation state."""

from __future__ import annotations

from collections.abc import Iterable
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
from .presentation_replacement import ReplacementBoundary
from .presentation_service import ServicePresentationModel as ServicePresentationModel
from .presentation_service import ServiceLine as ServiceLine
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
    segment_interaction_enabled,
)
from .wire import unwrap_variant, variant

VIEWPORT_BUFFER_LINES = 1_000
MAXIMUM_VIEWPORT_BUFFER_LINES = 10_000
MAXIMUM_VIEWPORT_UTF8_BYTES = 16 * 1024 * 1024
MAXIMUM_VIEWPORT_SEGMENTS = 250_000
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
    changed_from: int | None = 0
    trimmed_prefix: int = 0
    _button_generation: int | None = field(default=None, init=False, repr=False)
    _next_interaction_sequence: int = field(default=1, init=False, repr=False)
    _retired_interaction_sequence: int = field(default=0, init=False, repr=False)
    # Runtime TrimLines counts canonical rows that may already be outside this viewport.
    _hidden_prefix: int = field(default=0, init=False, repr=False)
    _line_indices: dict[int, int] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _line_index_offset: int = field(default=0, init=False, repr=False, compare=False)
    _retained_utf8_bytes: int = field(default=0, init=False, repr=False, compare=False)
    _retained_segments: int = field(default=0, init=False, repr=False, compare=False)
    _replacement: ReplacementBoundary = field(
        default_factory=ReplacementBoundary, init=False, repr=False, compare=False
    )

    def apply_snapshot(self, snapshot: dict[int, Any]) -> None:
        previous_sequences = self._collect_interaction_sequences(self.lines)
        self.revision = snapshot[0]
        self.title = snapshot[1]
        history = snapshot[2]
        self.lines = []
        self._retained_utf8_bytes = 0
        self._retained_segments = 0
        self._apply_settings(snapshot.get(6))
        # Snapshots carry each button's authoritative enabled state, but not the
        # current BREAKBUTTON generation. Wait for a generation delta before
        # filtering later partial line updates locally.
        self._button_generation = None
        raw_lines = history.get(0, [])
        retained_lines = self._budgeted_parsed_tail(raw_lines)
        self._hidden_prefix = len(raw_lines) - len(retained_lines)
        self.lines = [
            self._assign_interaction_sequences(line, previous_sequences)
            for line in retained_lines
        ]
        self._line_index_offset = 0
        self._rebuild_line_indices()
        self._recalculate_retained_cost()
        self._trim_viewport_lines()
        self.changed_from = 0
        self.trimmed_prefix = 0
        self.input_wait = snapshot.get(5)
        self._replacement.accept_snapshot()

    def apply_delta(self, delta: dict[int, Any]) -> None:
        if delta[0] != self.revision:
            raise ValueError(
                f"presentation delta starts at {delta[0]}, but local revision is {self.revision}"
            )
        for operation in delta[2]:
            tag, fields = unwrap_variant(operation)
            if not self._replacement.accepts_operation(tag):
                continue
            if tag == 0:
                self._mark_changed(len(self.lines))
                line = self._assign_interaction_sequences(parse_line(fields[0]))
                self._line_indices[line.line_id] = self._line_index_offset + len(self.lines)
                self.lines.append(line)
                line_bytes, line_segments = self._line_cost(line)
                self._retained_utf8_bytes += line_bytes
                self._retained_segments += line_segments
            elif tag == 1:
                requested = fields[0]
                count = min(requested, len(self.lines))
                if count:
                    for line in self.lines[-count:]:
                        self._line_indices.pop(line.line_id, None)
                        line_bytes, line_segments = self._line_cost(line)
                        self._retained_utf8_bytes -= line_bytes
                        self._retained_segments -= line_segments
                    del self.lines[-count:]
                    self._mark_changed(len(self.lines))
                self._hidden_prefix = max(0, self._hidden_prefix - (requested - count))
            elif tag == 2:
                self.lines.clear()
                self._hidden_prefix = 0
                self._line_indices.clear()
                self._line_index_offset = 0
                self._retained_utf8_bytes = 0
                self._retained_segments = 0
                self._mark_changed(0)
            elif tag == 3:
                self.title = fields[0]
            elif tag == 6:
                # minicbor omits an enum tuple field when Option is None. Consequently,
                # SetInputWait(None) is encoded as a zero-field variant, not `[None]`.
                self.input_wait = fields[0] if fields else None
            elif tag == 7:
                line_id, replacement = fields
                absolute_index = self._line_indices.get(line_id)
                if absolute_index is not None:
                    index = absolute_index - self._line_index_offset
                    previous_sequences = self._collect_interaction_sequences((self.lines[index],))
                    parsed = self._assign_interaction_sequences(
                        parse_line(replacement),
                        previous_sequences,
                    )
                    self._mark_changed(index)
                    previous_bytes, previous_segments = self._line_cost(self.lines[index])
                    parsed_bytes, parsed_segments = self._line_cost(parsed)
                    self.lines[index] = parsed
                    self._retained_utf8_bytes += parsed_bytes - previous_bytes
                    self._retained_segments += parsed_segments - previous_segments
                    if parsed.line_id != line_id:
                        self._line_indices.pop(line_id, None)
                        self._line_indices[parsed.line_id] = absolute_index
            elif tag == 8:
                self._apply_settings(fields[0])
            elif tag == 13:
                self._button_generation = int(fields[0])
                self._mark_changed(len(self.lines))
            elif tag == 14:
                requested = fields[0]
                hidden = min(requested, self._hidden_prefix)
                self._hidden_prefix -= hidden
                count = min(requested - hidden, len(self.lines))
                if count:
                    for line in self.lines[:count]:
                        self._line_indices.pop(line.line_id, None)
                        line_bytes, line_segments = self._line_cost(line)
                        self._retained_utf8_bytes -= line_bytes
                        self._retained_segments -= line_segments
                    del self.lines[:count]
                    self._line_index_offset += count
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
        self._line_indices = {
            line.line_id: self._line_index_offset + index for index, line in enumerate(self.lines)
        }

    def _trim_viewport_lines(self) -> None:
        count = max(0, len(self.lines) - self.maximum_physical_lines)
        retained_bytes = self._retained_utf8_bytes
        retained_segments = self._retained_segments
        for line in self.lines[:count]:
            line_bytes, line_segments = self._line_cost(line)
            retained_bytes -= line_bytes
            retained_segments -= line_segments
        while count < len(self.lines) and (
            retained_bytes > MAXIMUM_VIEWPORT_UTF8_BYTES
            or retained_segments > MAXIMUM_VIEWPORT_SEGMENTS
        ):
            line_bytes, line_segments = self._line_cost(self.lines[count])
            retained_bytes -= line_bytes
            retained_segments -= line_segments
            count += 1
        if count <= 0:
            return
        for line in self.lines[:count]:
            self._line_indices.pop(line.line_id, None)
            line_bytes, line_segments = self._line_cost(line)
            self._retained_utf8_bytes -= line_bytes
            self._retained_segments -= line_segments
        del self.lines[:count]
        self._hidden_prefix += count
        self._line_index_offset += count
        self.trimmed_prefix += count
        if self.changed_from is not None:
            self.changed_from = max(0, self.changed_from - count)

    def _apply_settings(self, settings: dict[int, Any] | None) -> None:
        if not settings:
            return
        self.background = color_hex(settings[2])
        self.button_focus = color_hex(settings[3])
        self.maximum_physical_lines = min(
            MAXIMUM_VIEWPORT_BUFFER_LINES,
            max(500, int(settings.get(4, VIEWPORT_BUFFER_LINES))),
        )
        self._trim_viewport_lines()

    def retire_history(self, revision: int | None = None) -> None:
        """Release the old game surface without breaking the next contiguous delta."""

        if revision is not None:
            self.revision = revision
        self.title = DEFAULT_PRESENTATION_TITLE
        self.lines.clear()
        self.input_wait = None
        self.changed_from = 0
        self.trimmed_prefix = 0
        self._button_generation = None
        self._hidden_prefix = 0
        self._line_indices.clear()
        self._line_index_offset = 0
        self._retained_utf8_bytes = 0
        self._retained_segments = 0
        self._replacement.begin()

    @staticmethod
    def _line_cost(line: DisplayLineModel) -> tuple[int, int]:
        return line._retained_utf8_bytes, line._retained_segments

    def _recalculate_retained_cost(self) -> None:
        self._retained_utf8_bytes = sum(
            line._retained_utf8_bytes for line in self.lines
        )
        self._retained_segments = sum(
            line._retained_segments for line in self.lines
        )

    def _budgeted_parsed_tail(
        self, raw_lines: list[dict[int, Any]]
    ) -> list[DisplayLineModel]:
        retained: list[DisplayLineModel] = []
        retained_bytes = 0
        retained_segments = 0
        first_retained = 0
        for raw_line in raw_lines[-self.maximum_physical_lines :]:
            line = parse_line(raw_line)
            line_bytes, line_segments = self._line_cost(line)
            retained.append(line)
            retained_bytes += line_bytes
            retained_segments += line_segments
            while first_retained < len(retained) and (
                retained_bytes > MAXIMUM_VIEWPORT_UTF8_BYTES
                or retained_segments > MAXIMUM_VIEWPORT_SEGMENTS
            ):
                removed_bytes, removed_segments = self._line_cost(
                    retained[first_retained]
                )
                retained_bytes -= removed_bytes
                retained_segments -= removed_segments
                first_retained += 1
        return retained[first_retained:]

    @property
    def button_generation(self) -> int | None:
        return self._button_generation

    @property
    def retired_interaction_sequence(self) -> int:
        return self._retired_interaction_sequence

    def retire_presented_interactions(self) -> int:
        """Retire the currently projected interaction sequence in constant time."""

        previous = self._retired_interaction_sequence
        self._retired_interaction_sequence = self._next_interaction_sequence - 1
        self._mark_changed(len(self.lines))
        return previous

    def restore_interaction_boundary(self, boundary: int) -> None:
        """Restore the sequence boundary saved before a rejected submission."""

        self._retired_interaction_sequence = boundary
        self._mark_changed(len(self.lines))

    def restore_submitted_interaction_boundary(self, boundary: int) -> bool:
        """Restore a submitted surface only when no newer interactions replaced it."""

        if self._retired_interaction_sequence != self._next_interaction_sequence - 1:
            return False
        self.restore_interaction_boundary(boundary)
        return True

    def segment_enabled(self, segment: DisplaySegment) -> bool:
        return segment_interaction_enabled(
            segment,
            self._button_generation,
            self._retired_interaction_sequence,
        )

    def _assign_interaction_sequences(
        self,
        line: DisplayLineModel,
        previous: dict[tuple[int, int], int] | None = None,
    ) -> DisplayLineModel:
        assigned: dict[tuple[int, int], int] = {}
        segments: list[DisplaySegment] = []
        for segment in line.segments:
            identity = self._token_identity(segment.token)
            if identity is None:
                segments.append(segment)
                continue
            sequence = assigned.get(identity)
            if sequence is None:
                sequence = (previous or {}).get(identity)
            if sequence is None:
                sequence = self._next_interaction_sequence
                self._next_interaction_sequence += 1
            assigned[identity] = sequence
            segments.append(replace(segment, interaction_sequence=sequence))
        projected = tuple(segments)
        return replace(line, segments=projected) if projected != line.segments else line

    @classmethod
    def _collect_interaction_sequences(
        cls, lines: Iterable[DisplayLineModel]
    ) -> dict[tuple[int, int], int]:
        sequences: dict[tuple[int, int], int] = {}
        for line in lines:
            for segment in line.segments:
                identity = cls._token_identity(segment.token)
                if identity is not None and segment.interaction_sequence is not None:
                    sequences[identity] = segment.interaction_sequence
        return sequences

    @staticmethod
    def _token_identity(token: dict[int, int] | None) -> tuple[int, int] | None:
        if token is None:
            return None
        return int(token[0]), int(token[1])

    def has_enabled_button(self, token: dict[int, int]) -> bool:
        """Return whether the current projection still exposes an activatable token."""

        return any(
            segment.token == token and self.segment_enabled(segment)
            for line in reversed(self.lines)
            for segment in reversed(line.segments)
        )


class PresentationDeltaAccumulator:
    """Incrementally reduce contiguous deltas without revisiting retained operations."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self._start_revision: int | None = None
        self._expected_revision: int | None = None
        self._operations: list[list[Any]] = []
        self._appended_lines: dict[int, int] = {}
        self._replaced_lines: dict[int, int] = {}
        self._state_operations: dict[int, int] = {}

    def add(self, delta: dict[int, Any]) -> None:
        if self._start_revision is None:
            self._start_revision = int(delta[0])
            self._expected_revision = int(delta[0])
        if delta[0] != self._expected_revision:
            raise ValueError(
                "presentation delta batch expected revision "
                f"{self._expected_revision}, got {delta[0]}"
            )
        self._expected_revision = int(delta[1])
        for operation in delta[2]:
            self._add_operation(operation)

    def _add_operation(self, operation: list[Any]) -> None:
        tag, fields = unwrap_variant(operation)
        if tag == 0:
            line_id = fields[0][0]
            self._appended_lines[line_id] = len(self._operations)
            self._replaced_lines.pop(line_id, None)
            self._operations.append(operation)
        elif tag == 7:
            line_id = fields[0]
            if line_id in self._appended_lines:
                self._operations[self._appended_lines[line_id]] = variant(0, fields[1])
            elif line_id in self._replaced_lines:
                self._operations[self._replaced_lines[line_id]] = operation
            else:
                self._replaced_lines[line_id] = len(self._operations)
                self._operations.append(operation)
        elif tag in (3, 4, 5, 6, 8, 9, 10, 11, 12):
            previous = self._state_operations.get(tag)
            if previous is None:
                self._state_operations[tag] = len(self._operations)
                self._operations.append(operation)
            else:
                self._operations[previous] = operation
        else:
            # Destructive line operations change the meaning of later IDs. Preserve their
            # order and start a fresh line-reduction region without rescanning prior output.
            self._appended_lines.clear()
            self._replaced_lines.clear()
            self._operations.append(operation)

    def take(self) -> dict[int, Any] | None:
        if self._start_revision is None or self._expected_revision is None:
            return None
        delta = {
            0: self._start_revision,
            1: self._expected_revision,
            2: self._operations,
        }
        self.clear()
        return delta


class PresentationEventAccumulator:
    """Retain the latest snapshot and its incrementally reduced following deltas."""

    def __init__(self) -> None:
        self._snapshot: dict[int, Any] | None = None
        self._deltas = PresentationDeltaAccumulator()

    def clear(self) -> None:
        self._snapshot = None
        self._deltas.clear()

    def replace_snapshot(self, snapshot: dict[int, Any]) -> None:
        self._snapshot = snapshot
        self._deltas.clear()

    def add_delta(self, delta: dict[int, Any]) -> None:
        self._deltas.add(delta)

    def take(self) -> tuple[dict[int, Any] | None, dict[int, Any] | None]:
        snapshot = self._snapshot
        delta = self._deltas.take()
        self._snapshot = None
        return snapshot, delta


def coalesce_presentation_deltas(deltas: list[dict[int, Any]]) -> dict[int, Any]:
    """Merge a contiguous one-shot batch with the incremental reducer."""

    if not deltas:
        raise ValueError("at least one presentation delta is required")
    accumulator = PresentationDeltaAccumulator()
    for delta in deltas:
        accumulator.add(delta)
    combined = accumulator.take()
    if combined is None:  # pragma: no cover - guarded by the non-empty check above
        raise AssertionError("non-empty presentation delta batch did not produce output")
    return combined


ServicePresentationModel.__module__ = __name__
