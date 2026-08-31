"""Private raw presentation-history projection for runtime service queries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .presentation_replacement import ReplacementBoundary
from .presentation_scene import apply_scene_delta, empty_scene, normalize_scene
from .presentation_text import plain_line
from .text_budget import utf8_length
from .wire import unwrap_variant


@dataclass(frozen=True, slots=True)
class ServiceLine:
    """Compact line projection retained for Runtime frontend text services."""

    line_id: int
    logical_line_start: bool
    alignment: int
    text: str


def _service_line(line: Mapping[int, object]) -> ServiceLine:
    return ServiceLine(
        line_id=int(line[0]),
        logical_line_start=bool(line.get(2, True)),
        alignment=int(line.get(4, 0)),
        text=plain_line(line),
    )


@dataclass(slots=True)
class ServicePresentationModel:
    """Compact worker-side text history used only for frontend service requests."""

    revision: int = 0
    lines: list[ServiceLine] = field(default_factory=list)
    input_wait: dict[int, object] | None = None
    scene: dict[int, object] = field(default_factory=empty_scene)
    _line_indices: dict[int, int] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _line_index_offset: int = field(default=0, init=False, repr=False, compare=False)
    _replacement: ReplacementBoundary = field(
        default_factory=ReplacementBoundary, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._line_indices = {line.line_id: index for index, line in enumerate(self.lines)}

    def apply_snapshot(self, snapshot: dict[int, object]) -> None:
        if 3 not in snapshot:
            raise ValueError("protocol 45 presentation snapshot is missing scene state")
        scene = normalize_scene(snapshot[3])
        revision = int(snapshot[0])
        history = snapshot[2]
        if not isinstance(history, Mapping):
            raise TypeError("presentation snapshot history must be a map")
        raw_lines = history.get(0, [])
        if not isinstance(raw_lines, list):
            raise TypeError("presentation snapshot lines must be a list")
        lines = [_service_line(line) for line in raw_lines]
        self.revision = revision
        self.lines = lines
        self.scene = scene
        self._line_index_offset = 0
        self._line_indices = {line.line_id: index for index, line in enumerate(self.lines)}
        input_wait = snapshot.get(5)
        self.input_wait = input_wait if isinstance(input_wait, dict) else None
        self._replacement.accept_snapshot()

    def apply_delta(self, delta: dict[int, object]) -> dict[int, object]:
        if delta[0] != self.revision:
            raise ValueError(
                f"presentation delta starts at {delta[0]}, but local revision is {self.revision}"
            )
        operations = delta[2]
        if not isinstance(operations, list):
            raise TypeError("presentation delta operations must be a list")
        filtered_operations: list[list[object]] = []
        for operation in operations:
            tag, fields = unwrap_variant(operation)
            if not self._replacement.accepts_operation(tag):
                continue
            filtered_operations.append(operation)
        candidate_scene = self.scene
        for operation in filtered_operations:
            tag, fields = unwrap_variant(operation)
            if tag == 4:
                if len(fields) != 1:
                    raise ValueError("presentation scene operation must contain one delta")
                candidate_scene = apply_scene_delta(candidate_scene, fields[0])
        for operation in filtered_operations:
            tag, fields = unwrap_variant(operation)
            if tag == 0:
                line = _service_line(fields[0])
                self._line_indices[line.line_id] = self._line_index_offset + len(self.lines)
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
                self._line_index_offset = 0
            elif tag == 6:
                self.input_wait = fields[0] if fields else None
            elif tag == 7:
                line_id, raw_line = fields
                absolute_index = self._line_indices.get(line_id)
                if absolute_index is not None:
                    index = absolute_index - self._line_index_offset
                    line = _service_line(raw_line)
                    self.lines[index] = line
                    if line.line_id != line_id:
                        self._line_indices.pop(line_id, None)
                        self._line_indices[line.line_id] = absolute_index
            elif tag == 14:
                count = min(fields[0], len(self.lines))
                if count:
                    for line in self.lines[:count]:
                        self._line_indices.pop(line.line_id, None)
                    del self.lines[:count]
                    self._line_index_offset += count
        self.scene = candidate_scene
        self.revision = delta[1]
        if len(filtered_operations) == len(operations):
            return delta
        return {**delta, 2: filtered_operations}

    def retire_history(self) -> int:
        """Release the old game history while preserving delta continuity."""

        self.lines.clear()
        self._line_indices.clear()
        self._line_index_offset = 0
        self.input_wait = None
        self._replacement.begin()
        return self.revision

    def reject_replacement(self, correlation_id: int | None) -> bool:
        return self._replacement.reject(correlation_id)

    def begin_replacement(self, message_id: int) -> int:
        revision = self.retire_history()
        self._replacement.begin(message_id)
        return revision

    def display_line(self, index: int) -> str:
        if 0 <= index < len(self.lines):
            return self.lines[index].text
        return ""

    def html_printed_str(self, line_number: int, maximum_utf8_bytes: int | None = None) -> str:
        if line_number < 0:
            return ""
        count = 0
        selected: list[ServiceLine] = []
        for line in reversed(self.lines):
            if count == line_number:
                selected.insert(0, line)
            if line.logical_line_start:
                count += 1
            if count > line_number:
                break
        if not selected:
            return ""
        alignment = {0: "left", 1: "center", 2: "right"}.get(selected[0].alignment, "left")
        if maximum_utf8_bytes is not None:
            wrapper_bytes = len(f"<p align='{alignment}'><nobr></nobr></p>")
            escaped_bytes = sum(_escaped_html_utf8_bytes(line.text) for line in selected)
            escaped_bytes += max(0, len(selected) - 1) * len(b"<br>")
            if wrapper_bytes + escaped_bytes > maximum_utf8_bytes:
                raise ValueError("printed HTML exceeds the frontend service limit")
        body = "<br>".join(_escape_html(line.text) for line in selected)
        return f"<p align='{alignment}'><nobr>{body}</nobr></p>"

    def physical_history(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def physical_history_utf8_bytes(self) -> int:
        return sum(utf8_length(line.text) + 1 for line in self.lines)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace(">", "&gt;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _escaped_html_utf8_bytes(text: str) -> int:
    return (
        utf8_length(text)
        + text.count("&") * 4
        + (text.count(">") + text.count("<")) * 3
        + (text.count('"') + text.count("'")) * 5
    )
