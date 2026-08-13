"""Frontend-owned project scanning and storage I/O."""

from __future__ import annotations

import json as json
import os as os
import tempfile as tempfile
import time as time
import unicodedata as unicodedata
from concurrent.futures import (
    FIRST_COMPLETED as FIRST_COMPLETED,
    Future as Future,
    ThreadPoolExecutor as ThreadPoolExecutor,
    wait as wait,
)
from dataclasses import asdict as asdict, dataclass as dataclass, field as field
from pathlib import Path as Path, PurePosixPath as PurePosixPath
from typing import (
    Any as Any,
    Callable as Callable,
    Sequence as Sequence,
    TypeVar as TypeVar,
    cast as cast,
)

import blake3 as blake3

from .frontend_io import (
    IO_ALREADY_EXISTS as IO_ALREADY_EXISTS,
    IO_CONFLICT as IO_CONFLICT,
    IO_INTERRUPTED as IO_INTERRUPTED,
    IO_INVALID_DATA as IO_INVALID_DATA,
    IO_NOT_FOUND as IO_NOT_FOUND,
    IO_OTHER as IO_OTHER,
    IO_PERMISSION_DENIED as IO_PERMISSION_DENIED,
    IO_READ_ONLY as IO_READ_ONLY,
    frontend_error as frontend_error,
)
from .storage import StorageBackend as StorageBackend
from .wire import variant as variant

FILE_CSV = 0
FILE_ERH = 1
FILE_ERB = 2
FILE_RESOURCE_MANIFEST = 3
FILE_RESOURCE = 4
FILE_CONFIGURATION = 5

RESOURCE_IMAGE_SUFFIXES = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
RESOURCE_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
RESOURCE_FONT_SUFFIXES = frozenset({".otf", ".ttc", ".ttf", ".woff", ".woff2"})
DEFAULT_MAXIMUM_ENVELOPE_BYTES = 128 * 1024 * 1024
DEFAULT_MAXIMUM_PAYLOAD_BYTES = 127 * 1024 * 1024
MAXIMUM_PROJECT_ENVELOPE_BYTES = 1024 * 1024 * 1024
PROJECT_ENVELOPE_HEADROOM_BYTES = 1024 * 1024
PROJECT_FILE_WIRE_OVERHEAD_BYTES = 256
ProjectScanProgress = Callable[[int, int], None]
ProjectConfigurationUpdate = Callable[[bytes, bytes, str], tuple[int, bytes]]
PROJECT_IO_WORKERS = min(8, os.cpu_count() or 1)
STABLE_READ_ATTEMPTS = 3
Input = TypeVar("Input")
Output = TypeVar("Output")


@dataclass(slots=True)
class ProjectScanMetrics:
    enumerate_ms: float = 0.0
    index_read_ms: float = 0.0
    index_write_ms: float = 0.0
    stat_ms: float = 0.0
    source_read_decode_hash_ms: float = 0.0
    source_index_present: bool = False
    source_files_reused: int = 0
    source_files_hashed: int = 0

    def telemetry(self) -> dict[str, float | bool | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _IndexedCandidate:
    path: Path
    category: int
    relative_path: str
    source_signature: tuple[int, int, int, int, int]
    content_hash: bytes | None
    content_size: int


class _ProjectChangedDuringScan(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectFile:
    relative_path: str
    category: int
    payload: list[Any] | None
    content_hash: bytes | None
    content_size: int = 0
    source_path: Path | None = None
    source_signature: tuple[int, int, int, int, int] | None = None

    def submitted(self) -> dict[int, Any]:
        if self.payload is None:
            raise RuntimeError(f"project file {self.relative_path} has not been materialized")
        result: dict[int, Any] = {
            0: self.relative_path,
            1: self.category,
            2: self.payload,
        }
        if self.content_hash is not None:
            result[3] = self.content_hash
        return result


from .project_scan import (  # noqa: E402
    _read_error_file as _read_error_file,
    _stable_read_project_file as _stable_read_project_file,
    _verify_stable_files as _verify_stable_files,
    _parallel_ordered as _parallel_ordered,
    _parallel_project_reads as _parallel_project_reads,
    _report_scan_progress as _report_scan_progress,
    _decode_project_source as _decode_project_source,
    classify_path as classify_path,
    _payload_size as _payload_size,
    read_project_file as read_project_file,
    _normalize_relative_path as _normalize_relative_path,
    _path_sort_key as _path_sort_key,
    _normalize_resource_manifest_paths as _normalize_resource_manifest_paths,
    _canonical_source_roots as _canonical_source_roots,
    _classify_project_path as _classify_project_path,
    _project_paths as _project_paths,
)
from .project_source_index import (  # noqa: E402
    _atomic_write_text as _atomic_write_text,
    _resource_manifest_lines as _resource_manifest_lines,
    _source_signature as _source_signature,
    _write_source_index as _write_source_index,
)
from .project_bundle import ProjectBundle as ProjectBundle  # noqa: E402

ProjectBundle.__module__ = __name__
