"""Structured frontend logs shared by the live view and diagnosis exports."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum
from typing import Iterable, Sequence

from rich.text import Text


class LogLevel(IntEnum):
    """Ordered log severity used as the log-view threshold."""

    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3

    @property
    def label(self) -> str:
        return {
            LogLevel.DEBUG: "DEBUG",
            LogLevel.INFO: "INFO ",
            LogLevel.WARNING: "WARN ",
            LogLevel.ERROR: "ERROR",
        }[self]

    @property
    def rich_style(self) -> str:
        return {
            LogLevel.DEBUG: "bold grey70",
            LogLevel.INFO: "bold white",
            LogLevel.WARNING: "bold #ffbf00",
            LogLevel.ERROR: "bold red",
        }[self]


_LEVEL_PREFIX = re.compile(
    r"^\s*(?:\[\s*(ERROR|WARN(?:ING)?|INFO|DEBUG)\s*\]\s*:?[ \t]*"
    r"|(ERROR|WARN(?:ING)?|INFO|DEBUG)(?:\s*:\s*|[ \t]+(?=\S)))",
    re.IGNORECASE,
)
_LEVEL_NAMES = {
    "DEBUG": LogLevel.DEBUG,
    "INFO": LogLevel.INFO,
    "WARN": LogLevel.WARNING,
    "WARNING": LogLevel.WARNING,
    "ERROR": LogLevel.ERROR,
}


@dataclass(frozen=True, slots=True)
class LogMessage:
    """A timestamp-free log event safe to pass through the worker queue."""

    level: LogLevel
    message: str
    authoritative: bool = False

    def __str__(self) -> str:
        return self.message

    def __contains__(self, value: str) -> bool:
        return value in self.message


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One timestamped log entry with equivalent rich and plain projections."""

    timestamp: str
    level: LogLevel
    message: str

    @property
    def plain_text(self) -> str:
        return f"[{self.timestamp}] {self.level.label} {self.message}"

    def render(self) -> Text:
        rendered = Text()
        rendered.append("[")
        rendered.append(self.timestamp, style="bold green")
        rendered.append("] ")
        rendered.append(self.level.label, style=self.level.rich_style)
        rendered.append(f" {self.message}")
        return rendered

    def __str__(self) -> str:
        return self.plain_text

    def __contains__(self, value: str) -> bool:
        return value in self.plain_text


MAX_LOG_ENTRIES = 4_096
MAX_LOG_UTF8_BYTES = 4 * 1024 * 1024
_TRUNCATION_MARKER = "…"


class BudgetedLogEntries(list[LogEntry]):
    """Retain the newest log entries within count and encoded-size budgets."""

    def __init__(
        self,
        entries: Iterable[LogEntry] = (),
        *,
        max_entries: int = MAX_LOG_ENTRIES,
        max_utf8_bytes: int = MAX_LOG_UTF8_BYTES,
    ) -> None:
        if max_entries < 1 or max_utf8_bytes < 1:
            raise ValueError("log budgets must be positive")
        super().__init__()
        self.max_entries = max_entries
        self.max_utf8_bytes = max_utf8_bytes
        self._entry_sizes: list[int] = []
        self._utf8_bytes = 0
        self.extend(entries)

    @property
    def utf8_bytes(self) -> int:
        return self._utf8_bytes

    def append(self, entry: LogEntry) -> None:
        entry = self._fit_entry(entry)
        # format_log_entries terminates every retained entry with one newline.
        size = len(entry.plain_text.encode("utf-8")) + 1
        super().append(entry)
        self._entry_sizes.append(size)
        self._utf8_bytes += size
        while len(self) > self.max_entries or self._utf8_bytes > self.max_utf8_bytes:
            self._utf8_bytes -= self._entry_sizes.pop(0)
            super().pop(0)

    def extend(self, entries: Iterable[LogEntry]) -> None:
        for entry in entries:
            self.append(entry)

    def clear(self) -> None:
        super().clear()
        self._entry_sizes.clear()
        self._utf8_bytes = 0

    def _fit_entry(self, entry: LogEntry) -> LogEntry:
        empty = replace(entry, message="")
        prefix_bytes = len(empty.plain_text.encode("utf-8")) + 1
        available = max(0, self.max_utf8_bytes - prefix_bytes)
        encoded = entry.message.encode("utf-8")
        if len(encoded) <= available:
            return entry
        marker = _TRUNCATION_MARKER.encode("utf-8")
        if available < len(marker):
            return empty
        message = encoded[: available - len(marker)].decode("utf-8", "ignore")
        return replace(entry, message=message + _TRUNCATION_MARKER)


def normalize_log_message(message: str, level: LogLevel) -> tuple[LogLevel, str]:
    """Remove repeated severity prefixes while retaining the strongest severity."""

    normalized = message.strip()
    while match := _LEVEL_PREFIX.match(normalized):
        name = next(group for group in match.groups() if group is not None).upper()
        level = max(level, _LEVEL_NAMES[name])
        normalized = normalized[match.end() :]
    return level, normalized


def make_log_entry(
    message: str,
    level: LogLevel = LogLevel.INFO,
    *,
    timestamp: str | None = None,
    authoritative: bool = False,
) -> LogEntry:
    if not authoritative:
        level, message = normalize_log_message(message, level)
    return LogEntry(timestamp or datetime.now().strftime("%H:%M:%S"), level, message)


def filter_log_entries(entries: Iterable[LogEntry], threshold: LogLevel) -> list[LogEntry]:
    return [entry for entry in entries if entry.level >= threshold]


def format_log_entries(entries: Sequence[LogEntry]) -> str:
    """Serialize every entry for diagnosis export, independent of the view threshold."""

    if not entries:
        return ""
    return "\n".join(entry.plain_text for entry in entries) + "\n"
