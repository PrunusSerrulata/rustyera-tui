"""Frontend-owned construction of portable fatal-error diagnosis archives."""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import zstandard


def resource_name(project: Path) -> str:
    """Return the resource folder component used to identify diagnosis files."""

    resolved = project.expanduser().resolve()
    return resolved.name or resolved.parent.name or "project"


def diagnosis_default_path(project: Path, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    name = resource_name(project)
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
    members = (
        ("runtime.snapshot", snapshot),
        ("runtime.log", logs.encode("utf-8")),
        (f"{project_name}-compiled-project.bin.zst", compiled_artifact),
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
