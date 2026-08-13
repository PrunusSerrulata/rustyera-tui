"""Frontend-owned construction of portable fatal-error diagnosis archives."""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

import zstandard


class _ProgressReader(io.BytesIO):
    """Report bytes actually consumed by tarfile without adding another payload pass."""

    def __init__(self, payload: bytes, report: Callable[[int], None]) -> None:
        super().__init__(payload)
        self._report = report

    def read(self, size: int = -1) -> bytes:
        data = super().read(size)
        if data:
            self._report(len(data))
        return data


def diagnosis_project_name(project_name: str) -> str:
    """Return a portable filename component for the project-defined title."""

    invalid = frozenset('<>:"/\\|?*')
    sanitized = "".join(
        "_" if character in invalid or ord(character) < 32 else character
        for character in project_name.strip()
    ).rstrip(". ")
    return sanitized or "project"


def diagnosis_default_path(
    project: Path,
    now: datetime | None = None,
    *,
    project_name: str = "",
) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    name = diagnosis_project_name(project_name)
    return project / f"{name}-diagnosis_{timestamp}.tar.zst"


def write_diagnosis_archive(
    target: Path,
    *,
    project_name: str,
    snapshot: bytes,
    input_replay: bytes,
    logs: str,
    project_file: bytes,
    exported_at: datetime | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Atomically write a zstd-compressed tar archive without exposing partial output."""

    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    timestamp = int((exported_at or datetime.now()).timestamp())
    project_name = diagnosis_project_name(project_name)
    members = (
        ("runtime.snapshot", snapshot),
        ("runtime.log", logs.encode("utf-8")),
        ("input-replay.jsonl", input_replay),
        (f"{project_name}.reraproj", project_file),
    )
    progress_total = sum(len(payload) for _, payload in members)
    progress_completed = 0
    last_reported_percent = 0

    def record_progress(consumed: int) -> None:
        nonlocal progress_completed, last_reported_percent
        progress_completed += consumed
        if progress is None or progress_total <= 0 or progress_completed >= progress_total:
            return
        percent = progress_completed * 100 // progress_total
        if percent > last_reported_percent:
            last_reported_percent = percent
            progress(progress_completed, progress_total)

    try:
        if progress is not None:
            progress(0, progress_total)
        with os.fdopen(descriptor, "wb") as output:
            compressor = zstandard.ZstdCompressor(level=3).stream_writer(output, closefd=False)
            with (
                compressor,
                tarfile.open(fileobj=compressor, mode="w|", format=tarfile.PAX_FORMAT) as archive,
            ):
                for name, payload in members:
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mtime = timestamp
                    info.mode = 0o600
                    archive.addfile(info, _ProgressReader(payload, record_progress))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        if progress is not None:
            progress(progress_total, progress_total)
    finally:
        Path(temporary).unlink(missing_ok=True)
