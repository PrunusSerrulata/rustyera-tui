"""Structured frontend logs shared by the live view and diagnosis exports."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        rendered.append(f"[{self.timestamp}]", style="bold green")
        rendered.append(" ")
        rendered.append(self.level.label, style=self.level.rich_style)
        rendered.append(f" {self.message}")
        return rendered

    def __str__(self) -> str:
        return self.plain_text

    def __contains__(self, value: str) -> bool:
        return value in self.plain_text


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
