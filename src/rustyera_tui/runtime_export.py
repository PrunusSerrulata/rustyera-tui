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
    input_replay: bytes | Path | None = None
    snapshot: bytes | Path | None = None
    project_file: bytes | Path | None = None
    temporary_directory: Path | None = None
    stage: str = "export_wait"
    retry_after_ns: int = 0

    @classmethod
    def create(cls, target: Path, project_name: str, logs: str) -> DiagnosisExport:
        resolved = target.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=f".{resolved.name}.parts.", dir=resolved.parent))
        return cls(resolved, project_name, logs, temporary_directory=directory)

    def part_path(self, name: str) -> Path:
        if self.temporary_directory is None:
            raise RuntimeError("diagnosis temporary directory is missing")
        return self.temporary_directory / name

    def cleanup(self) -> None:
        directory = self.temporary_directory
        self.temporary_directory = None
        for path in (self.input_replay, self.snapshot, self.project_file):
            if isinstance(path, Path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.input_replay = None
        self.snapshot = None
        self.project_file = None
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass


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

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self.stream.write(data) != len(data):
            raise OSError("export was not written completely")
        self.hasher.update(data)
        self.received += len(data)

    def finish(self, expected_size: int, expected_digest: bytes) -> None:
        try:
            if self.received != expected_size or self.hasher.digest() != expected_digest:
                raise RuntimeError("export digest verification failed")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            os.replace(self.temporary, self.target)
        except BaseException:
            self.cancel()
            raise

    def cancel(self) -> None:
        try:
            if not self.stream.closed:
                self.stream.close()
        except (OSError, ValueError):
            pass
        try:
            self.temporary.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass(slots=True)
class _PendingExport:
    """Own every resource and protocol marker for one active export."""

    path: Path
    stage: ExportStage
    stream: AtomicExportStream
    descriptor: dict[int, Any] | None = None
    message_id: int | None = None
    closed: bool = False

    @classmethod
    def open(cls, path: Path, stage: ExportStage) -> _PendingExport:
        return cls(path, stage, AtomicExportStream.open(path))

    def finish(self) -> Path:
        if self.closed:
            return self.path
        try:
            descriptor = self.descriptor
            if descriptor is None:
                raise RuntimeError("state export descriptor is missing")
            self.stream.finish(int(descriptor[2]), bytes(descriptor[3]))
        except BaseException:
            self.cancel()
            raise
        else:
            self.closed = True
        return self.path

    def cancel(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stream.cancel()


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
    delete_path_when_finished: bool = False


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
