"""Frontend-observable runtime diagnostics and log projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.cells import cell_len

from .log_model import LogLevel, LogMessage
from .protocol_text import FAULT_CODES, enum_text
from .runtime_types import FrontendEvent
from .compatibility import compatibility_context


def log_event(
    message: str,
    level: LogLevel = LogLevel.INFO,
    *,
    authoritative: bool = False,
) -> FrontendEvent:
    return FrontendEvent("log", LogMessage(level, message, authoritative))


def runtime_log_level(value: Any) -> LogLevel:
    return LogLevel(value)


def format_project_diagnostic(diagnostic: dict[int, Any], source_text: str | None = None) -> str:
    """Render a structured compiler diagnostic without changing its UTF-8 byte offsets."""

    code = str(diagnostic.get(0, "unknown"))
    message = str(diagnostic.get(2, ""))
    context = compatibility_context(diagnostic.get(5))
    if context:
        message = f"{message} [{context}]"
    source = diagnostic.get(3)
    if not isinstance(source, dict):
        return f"[{code}]: {message}"

    path = str(source.get(0, "<unknown>"))
    line = source.get(3)
    byte_column = source.get(4)
    excerpt = None
    marker = None
    if source_text is not None:
        encoded = source_text.encode("utf-8")
        start = min(max(int(source.get(1, 0)), 0), len(encoded))
        end = min(max(int(source.get(2, start)), start), len(encoded))
        line_start = encoded.rfind(b"\n", 0, start) + 1
        line_end = encoded.find(b"\n", start)
        if line_end < 0:
            line_end = len(encoded)
        line = encoded.count(b"\n", 0, start)
        byte_column = start - line_start
        raw_line = encoded[line_start:line_end].removesuffix(b"\r")
        raw_prefix = encoded[line_start:start]
        raw_highlight = encoded[start : min(end, line_end)]
        excerpt = raw_line.decode("utf-8", errors="replace").expandtabs(4)
        prefix = raw_prefix.decode("utf-8", errors="ignore").expandtabs(4)
        through_highlight = (
            (raw_prefix + raw_highlight).decode("utf-8", errors="ignore").expandtabs(4)
        )
        marker_start = cell_len(prefix)
        width = max(1, cell_len(through_highlight) - marker_start)
        marker = f"{' ' * marker_start}^{'~' * (width - 1)}"

    display_line = int(line) + 1 if isinstance(line, int) else "?"
    display_column = int(byte_column) + 1 if isinstance(byte_column, int) else "?"
    header = f"{path}:{display_line}:{display_column}: [{code}]: {message}"
    if excerpt is None or marker is None:
        return header
    return f"{header}\n    {excerpt}\n    {marker}"


@dataclass(frozen=True, slots=True)
class RuntimeFailure:
    """Terminal runtime fault retained as structured frontend-observable state."""

    code: int
    message: str
    command: str | None = None
    function: str | None = None
    source_path: str | None = None
    source_line: int | None = None
    compatibility: str = ""

    def display(self) -> str:
        location = ""
        if self.source_path:
            location = f"（{self.source_path}"
            if self.source_line is not None:
                location += f":{self.source_line}"
            location += "）"
        context = f" [{self.function}]" if self.function else ""
        code = enum_text(self.code, FAULT_CODES, "FaultCode")
        compatibility = f" [{self.compatibility}]" if self.compatibility else ""
        return f"Runtime 故障 [{code}]{context}：{self.message}{location}{compatibility}"
