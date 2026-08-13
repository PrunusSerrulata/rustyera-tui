"""Private raw presentation-history projection for runtime service queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .wire import unwrap_variant


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
