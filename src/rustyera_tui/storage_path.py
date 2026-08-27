"""Snake Data path identity without changing the filesystem's existing spelling."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
import unicodedata
from pathlib import Path, PurePosixPath

from .resource_storage import resource_path

MAXIMUM_PATH_DEPTH = 256
MAXIMUM_LOOKUP_ENTRIES = 100_000
MAXIMUM_LOOKUP_PATH_BYTES = 8 * 1024 * 1024


def directory_entries(path: Path) -> Iterator[Path]:
    # Path.iterdir implementations may materialize every name before caller budgets run.
    with os.scandir(path) as entries:
        for entry in entries:
            yield Path(entry.path)


def normalized_data_path(root: Path, relative: str) -> Path:
    return resolve_data_path(root, relative).canonical


@dataclass(frozen=True, slots=True)
class ResolvedDataPath:
    canonical: Path
    logical: str
    exists: bool = False


def validate_actual_basename(name: str) -> None:
    """Validate an actual directory entry before treating it as a protocol component."""
    if (
        not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name
        or (len(name) > 1 and name[1] == ":")
        or len(name.encode("utf-8")) > 4096
    ):
        raise ValueError("storage directory contains an invalid name")


def normalized_basename(name: str) -> str:
    validate_actual_basename(name)
    return unicodedata.normalize("NFC", name)


def resolve_data_path(root: Path, relative: str) -> ResolvedDataPath:
    parts = PurePosixPath(resource_path(relative)).parts
    if len(parts) > MAXIMUM_PATH_DEPTH:
        raise ValueError("storage path exceeds the directory depth limit")
    try:
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise PermissionError("Data namespace root cannot be a symbolic link")
        return _resolve_components(root.resolve(), parts, metadata is not None)
    except RuntimeError as error:
        raise ValueError("storage path contains a symbolic-link loop") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("storage path contains a symbolic-link loop") from error
        raise


def _resolve_components(root: Path, parts: tuple[str, ...], root_exists: bool) -> ResolvedDataPath:
    current = root
    logical: list[str] = []
    visited: set[tuple[int, int]] = set()
    scanned = 0
    path_bytes = 0
    for index, part in enumerate(parts):
        try:
            metadata = current.stat()
        except FileNotFoundError:
            if index > 0 or root_exists:
                raise ValueError("storage directory changed during path lookup") from None
            # No directory is created until every existing prefix has been validated.
            return _resolved_path(root, current.joinpath(*parts[index:]), [*logical, *parts[index:]])
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("storage path component is not a directory")
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in visited:
            raise ValueError("storage path contains a directory link loop")
        visited.add(identity)
        names: set[str] = set()
        selected: Path | None = None
        selected_name = ""
        try:
            for candidate in directory_entries(current):
                scanned += 1
                canonical = normalized_basename(candidate.name)
                relative_candidate = PurePosixPath(*logical, canonical)
                candidate_bytes = len(relative_candidate.as_posix().encode("utf-8"))
                if candidate_bytes > 4096 or len(relative_candidate.parts) > MAXIMUM_PATH_DEPTH:
                    raise ValueError("storage path exceeds the path limit")
                path_bytes += candidate_bytes
                if scanned > MAXIMUM_LOOKUP_ENTRIES or path_bytes > MAXIMUM_LOOKUP_PATH_BYTES:
                    raise ValueError("storage path lookup exceeds the scan limit")
                key = canonical.lower()
                if key in names:
                    raise ValueError("storage directory contains duplicate normalized names")
                names.add(key)
                if key == part.lower():
                    selected = candidate
                    selected_name = canonical
            after = current.stat()
            if (after.st_dev, after.st_ino) != identity:
                raise ValueError("storage directory changed during path lookup")
        except FileNotFoundError as error:
            raise ValueError("storage directory changed during path lookup") from error
        if selected is None:
            return _resolved_path(root, current.joinpath(*parts[index:]), [*logical, *parts[index:]])
        try:
            current = selected.resolve(strict=True)
        except FileNotFoundError as error:
            # A selected dangling link is corrupt input, not an absent Data overlay.
            raise ValueError("storage path changed or contains a dangling link") from error
        if current != root and root not in current.parents:
            raise PermissionError("storage path escapes its namespace")
        try:
            metadata = current.stat()
        except FileNotFoundError as error:
            raise ValueError("storage path changed during lookup") from error
        if stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) in visited:
            raise ValueError("storage path contains a directory link loop")
        logical.append(selected_name)
    return _resolved_path(root, current, logical, bool(parts) or root_exists)


def _resolved_path(
    root: Path, current: Path, logical: list[str], exists: bool = False
) -> ResolvedDataPath:
    relative = PurePosixPath(*logical).as_posix() if logical else ""
    if len(logical) > MAXIMUM_PATH_DEPTH or len(relative.encode("utf-8")) > 4096:
        raise ValueError("logical storage path exceeds the path limit")
    return ResolvedDataPath(_bounded_path(root, current), relative, exists)


def _bounded_path(root: Path, current: Path) -> Path:
    relative = current.relative_to(root)
    if len(relative.parts) > MAXIMUM_PATH_DEPTH or len(relative.as_posix().encode("utf-8")) > 4096:
        raise ValueError("resolved storage path exceeds the path limit")
    return current
