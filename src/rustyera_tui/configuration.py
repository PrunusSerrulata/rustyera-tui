"""Typed frontend projection of the public emuera.config wire contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TUI_CLIENT = 1 << 1


@dataclass(frozen=True, slots=True)
class ConfigurationEntry:
    code: str
    japanese: str
    english: str
    value: str
    kind: int
    allowed: tuple[str, ...]
    fixed: bool
    applicability: int

    @classmethod
    def from_wire(cls, value: Any) -> ConfigurationEntry:
        if not isinstance(value, dict):
            raise ValueError("configuration entry is not a map")
        code = value.get(0)
        japanese = value.get(1)
        english = value.get(2)
        current = value.get(3)
        kind = value.get(4)
        allowed = value.get(5)
        fixed = value.get(6)
        applicability = value.get(7)
        if not all(isinstance(item, str) for item in (code, japanese, english, current)):
            raise ValueError("configuration entry has invalid text fields")
        if not isinstance(kind, int) or not 0 <= kind <= 7:
            raise ValueError("configuration entry has an invalid value kind")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("configuration entry has invalid allowed values")
        if not isinstance(fixed, bool) or not isinstance(applicability, int):
            raise ValueError("configuration entry has invalid flags")
        return cls(
            code,
            japanese,
            english,
            current,
            kind,
            tuple(allowed),
            fixed,
            applicability,
        )

    @property
    def label(self) -> str:
        return self.japanese or self.english or self.code


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    project_revision: int
    source_digest: bytes
    entries: tuple[ConfigurationEntry, ...]

    @classmethod
    def from_wire(cls, value: Any) -> ConfigurationSnapshot:
        if not isinstance(value, dict):
            raise ValueError("configuration snapshot is not a map")
        revision = value.get(0)
        digest = value.get(1)
        entries = value.get(2)
        if not isinstance(revision, int) or not isinstance(digest, bytes):
            raise ValueError("configuration snapshot has invalid identity")
        if not isinstance(entries, list):
            raise ValueError("configuration snapshot has invalid entries")
        return cls(revision, digest, tuple(ConfigurationEntry.from_wire(item) for item in entries))

    @property
    def tui_entries(self) -> tuple[ConfigurationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.applicability & TUI_CLIENT)

    def value(self, code: str, default: str) -> str:
        return next((entry.value for entry in self.entries if entry.code == code), default)

    def prepare_wire(self, changes: list[ConfigurationChange]) -> dict[int, Any]:
        return {
            0: self.project_revision,
            1: self.source_digest,
            2: [change.to_wire() for change in changes],
        }


@dataclass(frozen=True, slots=True)
class ConfigurationChange:
    code: str
    value: str

    def to_wire(self) -> dict[int, str]:
        return {0: self.code, 1: self.value}


@dataclass(frozen=True, slots=True)
class PreparedConfiguration:
    project_revision: int
    expected_source_digest: bytes
    contents: str
    restart_required: bool

    @classmethod
    def from_wire(cls, value: Any) -> PreparedConfiguration:
        if not isinstance(value, dict):
            raise ValueError("prepared configuration is not a map")
        revision = value.get(0)
        digest = value.get(1)
        contents = value.get(2)
        restart = value.get(3)
        if (
            not isinstance(revision, int)
            or not isinstance(digest, bytes)
            or not isinstance(contents, str)
            or not isinstance(restart, bool)
        ):
            raise ValueError("prepared configuration has invalid fields")
        return cls(revision, digest, contents, restart)
