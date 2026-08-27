"""Bounded storage enumeration with logical paths and explicit traversal failures."""

from __future__ import annotations

import errno
import fnmatch
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from .storage_path import (
    MAXIMUM_PATH_DEPTH,
    ResolvedDataPath,
    directory_entries,
    validate_actual_basename,
)
from .storage_pattern import SnakeStoragePattern
from .storage_state import _change_token

MAXIMUM_LIST_ENTRIES = 100_000
MAXIMUM_LIST_PATH_BYTES = 8 * 1024 * 1024


def list_storage(
    root: Path,
    selected: ResolvedDataPath,
    pattern: str | None,
    recursive: bool,
    normalized_identity: bool,
) -> list[dict[int, Any]]:
    matcher = SnakeStoragePattern(pattern) if normalized_identity else None
    try:
        selected.canonical.lstat()
    except FileNotFoundError as error:
        if selected.exists:
            raise ValueError("storage listing target disappeared after path lookup") from error
        return []
    try:
        return _walk_storage(root, selected, pattern, recursive, matcher)
    except FileNotFoundError as error:
        # Once the target has been observed, missing children/links are not a missing namespace.
        raise ValueError("storage listing changed or contains a dangling link") from error
    except RuntimeError as error:
        raise ValueError("storage listing contains a symbolic-link loop") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("storage listing contains a symbolic-link loop") from error
        raise


def _walk_storage(
    root: Path,
    selected: ResolvedDataPath,
    pattern: str | None,
    recursive: bool,
    matcher: SnakeStoragePattern | None,
) -> list[dict[int, Any]]:
    entries: list[dict[int, Any]] = []
    normalized: set[str] = set()
    visited: set[tuple[int, int]] = set()
    pending = [(selected.canonical, selected.logical)]
    visited_entries = visited_path_bytes = path_bytes = 0
    while pending:
        directory, prefix = pending.pop()
        resolved = directory.resolve(strict=True)
        _check_containment(root, resolved)
        metadata = resolved.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("storage listing target is not a directory")
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in visited:
            raise ValueError("storage listing contains a directory link loop or alias")
        visited.add(identity)
        for candidate in directory_entries(directory):
            visited_entries += 1
            if visited_entries > MAXIMUM_LIST_ENTRIES:
                raise ValueError("storage listing exceeds the response limit")
            validate_actual_basename(candidate.name)
            name = unicodedata.normalize("NFC", candidate.name) if matcher is not None else candidate.name
            logical = f"{prefix}/{name}" if prefix else name
            logical_bytes = len(logical.encode("utf-8"))
            if logical_bytes > 4096 or len(PurePosixPath(logical).parts) > MAXIMUM_PATH_DEPTH:
                raise ValueError("storage listing exceeds the path limit")
            visited_path_bytes += logical_bytes
            if visited_path_bytes > MAXIMUM_LIST_PATH_BYTES:
                raise ValueError("storage listing exceeds the traversal path limit")
            if matcher is not None:
                key = logical.lower()
                if key in normalized:
                    raise ValueError("storage listing has duplicate normalized paths")
                normalized.add(key)
            is_link = stat.S_ISLNK(candidate.lstat().st_mode)
            target = candidate.resolve(strict=True)
            _check_containment(root, target)
            metadata = target.stat()
            if stat.S_ISDIR(metadata.st_mode):
                if target == resolved or target in resolved.parents:
                    raise ValueError("storage listing contains a directory link loop")
                # Reference rglob did not follow directory symlinks. Snake accepts a safe
                # alias but still rejects repeated real directories during a single walk.
                if recursive and (matcher is not None or not is_link):
                    pending.append((candidate, logical))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if matcher is not None:
                if not matcher.matches(name):
                    continue
            elif pattern and not fnmatch.fnmatch(name, pattern):
                continue
            path_bytes += logical_bytes
            if len(entries) >= MAXIMUM_LIST_ENTRIES or path_bytes > MAXIMUM_LIST_PATH_BYTES:
                raise ValueError("storage listing exceeds the response limit")
            metadata = candidate.stat()
            if candidate.resolve(strict=True) != target:
                raise ValueError("storage file changed during listing")
            entries.append({0: logical, 1: metadata.st_size, 2: None, 3: _change_token(metadata)})
        after = directory.stat()
        if (after.st_dev, after.st_ino) != identity:
            raise ValueError("storage directory changed during listing")
    entries.sort(key=lambda item: item[0])
    return entries


def _check_containment(root: Path, path: Path) -> None:
    if path != root and root not in path.parents:
        raise PermissionError("storage listing escaped its namespace")
