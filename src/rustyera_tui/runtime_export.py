"""Runtime export state and crash-safe file writing."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

import blake3


class ExportStage(IntEnum):
    SNAPSHOT = 1
    COMPILED_CACHE = 2
    DIAGNOSIS_SNAPSHOT = 3
    DIAGNOSIS_PROJECT = 4
    PROJECT_FILE = 5
    DIAGNOSIS_REPLAY = 6
    INPUT_REPLAY = 7


RUNTIME_EXPORT_KIND = {
    ExportStage.SNAPSHOT: 1,
    ExportStage.COMPILED_CACHE: 2,
    ExportStage.DIAGNOSIS_SNAPSHOT: 1,
    ExportStage.DIAGNOSIS_PROJECT: 3,
    ExportStage.PROJECT_FILE: 3,
    ExportStage.DIAGNOSIS_REPLAY: 4,
    ExportStage.INPUT_REPLAY: 4,
}
DIAGNOSIS_EXPORT_STAGES = {
    ExportStage.DIAGNOSIS_REPLAY,
    ExportStage.DIAGNOSIS_SNAPSHOT,
    ExportStage.DIAGNOSIS_PROJECT,
}


@dataclass(slots=True)
class DiagnosisExport:
    target: Path
    project_name: str
    logs: str
    input_replay: bytes | None = None
    snapshot: bytes | None = None
    project_file: bytes | None = None
    stage: str = "export_wait"
    retry_after_ns: int = 0


@dataclass(slots=True)
class AtomicExportStream:
    target: Path
    temporary: Path
    stream: Any
    hasher: Any
    received: int = 0

    @classmethod
    def open(cls, target: Path) -> AtomicExportStream:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        return cls(target, Path(temporary), os.fdopen(descriptor, "wb"), blake3.blake3())

    def write(self, data: bytes) -> None:
        if self.stream.write(data) != len(data):
            raise OSError("project export was not written completely")
        self.hasher.update(data)
        self.received += len(data)

    def finish(self, expected_size: int, expected_digest: bytes) -> None:
        if self.received != expected_size or self.hasher.digest() != expected_digest:
            raise RuntimeError("project export digest verification failed")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        os.replace(self.temporary, self.target)

    def cancel(self) -> None:
        if not self.stream.closed:
            self.stream.close()
        self.temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class FullProjectExport:
    target: Path
    stream: AtomicExportStream
    retry_after_ns: int = 0


@dataclass(slots=True)
class PendingStateImport:
    kind: int
    purpose: str
    total_bytes: int
    payload: bytes | None = None
    path: Path | None = None
    begin_message_id: int | None = None
    transfer_id: int | None = None
    commit_message_id: int | None = None
    command_message_ids: set[int] = field(default_factory=set)


def atomic_write(path: Path, data: bytes | bytearray) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
