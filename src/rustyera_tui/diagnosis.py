"""Frontend-owned construction of portable fatal-error diagnosis archives."""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import zstandard


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
    logs: str,
    compiled_artifact: bytes,
    exported_at: datetime | None = None,
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
        (f"{project_name}.reraproj", compiled_artifact),
    )
    try:
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
                    archive.addfile(info, io.BytesIO(payload))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
