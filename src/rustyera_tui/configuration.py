"""Typed frontend projection of the public reraconfig.toml wire contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TUI_CLIENT = 1 << 1
APPLICATION_HOT = 0
APPLICATION_RESTART = 1


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
    default_value: str
    effective_value: str
    application: int

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
        default_value = value.get(8)
        effective_value = value.get(9)
        application = value.get(10)
        if not all(isinstance(item, str) for item in (code, japanese, english, current)):
            raise ValueError("configuration entry has invalid text fields")
        if not isinstance(kind, int) or not 0 <= kind <= 7:
            raise ValueError("configuration entry has an invalid value kind")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("configuration entry has invalid allowed values")
        if not isinstance(fixed, bool) or not isinstance(applicability, int):
            raise ValueError("configuration entry has invalid flags")
        if not isinstance(default_value, str) or not isinstance(effective_value, str):
            raise ValueError("configuration entry has invalid defaults")
        if application not in (APPLICATION_HOT, APPLICATION_RESTART):
            raise ValueError("configuration entry has invalid application policy")
        return cls(
            code,
            japanese,
            english,
            current,
            kind,
            tuple(allowed),
            fixed,
            applicability,
            default_value,
            effective_value,
            application,
        )

    @property
    def label(self) -> str:
        return self.japanese or self.english or self.code


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    project_revision: int
    source_digest: bytes
    entries: tuple[ConfigurationEntry, ...]
    restart_pending: bool
    generated_source: str | None

    @classmethod
    def from_wire(cls, value: Any) -> ConfigurationSnapshot:
        if not isinstance(value, dict):
            raise ValueError("configuration snapshot is not a map")
        revision = value.get(0)
        digest = value.get(1)
        entries = value.get(2)
        restart_pending = value.get(3)
        generated_source = value.get(4)
        if not isinstance(revision, int) or not isinstance(digest, bytes):
            raise ValueError("configuration snapshot has invalid identity")
        if not isinstance(entries, list):
            raise ValueError("configuration snapshot has invalid entries")
        if not isinstance(restart_pending, bool):
            raise ValueError("configuration snapshot has invalid restart status")
        if generated_source is not None and not isinstance(generated_source, str):
            raise ValueError("configuration snapshot has invalid generated source")
        return cls(
            revision,
            digest,
            tuple(ConfigurationEntry.from_wire(item) for item in entries),
            restart_pending,
            generated_source,
        )

    @property
    def tui_entries(self) -> tuple[ConfigurationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.applicability & TUI_CLIENT)

    def value(self, code: str, default: str) -> str:
        return next((entry.value for entry in self.entries if entry.code == code), default)

    def effective_value(self, code: str, default: str) -> str:
        return next(
            (entry.effective_value for entry in self.entries if entry.code == code),
            default,
        )

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
    prepared_source_digest: bytes

    @classmethod
    def from_wire(cls, value: Any) -> PreparedConfiguration:
        if not isinstance(value, dict):
            raise ValueError("prepared configuration is not a map")
        revision = value.get(0)
        digest = value.get(1)
        contents = value.get(2)
        restart = value.get(3)
        prepared_digest = value.get(4)
        if (
            not isinstance(revision, int)
            or not isinstance(digest, bytes)
            or not isinstance(contents, str)
            or not isinstance(restart, bool)
            or not isinstance(prepared_digest, bytes)
        ):
            raise ValueError("prepared configuration has invalid fields")
        return cls(revision, digest, contents, restart, prepared_digest)
