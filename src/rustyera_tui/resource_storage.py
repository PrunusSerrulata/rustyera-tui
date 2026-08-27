"""Read-only storage projection of the committed project resource manifest."""

from __future__ import annotations

import fnmatch
import unicodedata
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import blake3

from .frontend_io import IO_CONFLICT, IO_READ_ONLY
from .storage_state import _change_token
from .storage_pattern import SnakeStoragePattern
from .wire import variant

if TYPE_CHECKING:
    from .project import ProjectFile
    from .project_bundle import ProjectBundle

MAXIMUM_FULL_READ_BYTES = 64 * 1024 * 1024
MAXIMUM_RANGE_READ_BYTES = 4 * 1024 * 1024
MAXIMUM_LIST_ENTRIES = 100_000
MAXIMUM_LIST_PATH_BYTES = 8 * 1024 * 1024
MAXIMUM_PATH_BYTES = 4096


def resource_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\0" in normalized
        or (len(normalized) > 1 and normalized[1] == ":")
        or len(normalized.encode("utf-8")) > MAXIMUM_PATH_BYTES
    ):
        raise ValueError("resource path is not a safe relative path")
    return "" if str(path) == "." else path.as_posix()


class ResourceStorage:
    def __init__(self, bundle: ProjectBundle):
        self.bundle = bundle

    def operate(self, relative: str, operation: int, fields: list[Any]) -> list[Any]:
        if operation in (1, 3):
            return variant(4, {0: IO_READ_ONLY, 1: "Resource storage is read-only"})
        relative = resource_path(relative)
        # Snapshot the committed entries before I/O; pending reload candidates never authorize reads.
        files = {}
        manifest_path_bytes = 0
        for item in self.bundle.files.values():
            if item.category != 4:
                continue
            canonical = resource_path(item.relative_path)
            manifest_path_bytes += len(canonical.encode("utf-8"))
            if len(files) >= MAXIMUM_LIST_ENTRIES or manifest_path_bytes > MAXIMUM_LIST_PATH_BYTES:
                raise ValueError("resource manifest exceeds the response limit")
            key = canonical.lower()
            if key in files:
                raise ValueError("resource manifest contains duplicate normalized paths")
            files[key] = (canonical, item)
        if operation == 2:
            pattern, recursive = fields
            matcher = (
                SnakeStoragePattern(pattern)
                if self.bundle.compatibility_profile == "emuera.skia.snake" else None
            )
            if pattern and len(pattern.encode("utf-8")) > MAXIMUM_PATH_BYTES:
                raise ValueError("resource listing pattern exceeds the response limit")
            prefix = relative.lower() + "/" if relative else ""
            entries = []
            path_bytes = 0
            for key, (canonical, item) in sorted(files.items(), key=lambda pair: pair[1][0]):
                if not key.startswith(prefix):
                    continue
                tail = "/".join(canonical.split("/")[len(relative.split("/")) if relative else 0:])
                if not tail or (not recursive and "/" in tail):
                    continue
                name = PurePosixPath(tail).name
                if matcher is not None:
                    if not matcher.matches(name):
                        continue
                elif pattern and not fnmatch.fnmatchcase(name, pattern):
                    continue
                path_bytes += len(canonical.encode("utf-8"))
                if len(entries) >= MAXIMUM_LIST_ENTRIES or path_bytes > MAXIMUM_LIST_PATH_BYTES:
                    raise ValueError("resource listing exceeds the response limit")
                try:
                    observed = self._read(item, 0, 0)
                except FileNotFoundError:
                    # A committed manifest entry disappearing is a conflict, not absence.
                    return self._conflict()
                if isinstance(observed, list):
                    return observed
                _, digest, token = observed
                entries.append({0: canonical, 1: item.content_size, 2: digest, 3: token})
            return variant(2, entries)
        found = files.get(relative.lower())
        if found is None:
            raise PermissionError("resource is not authorized by the active project manifest")
        _, item = found
        offset, maximum, expected = 0, 0, None
        if operation == 0:
            if item.content_size > MAXIMUM_FULL_READ_BYTES:
                raise ValueError("resource full read exceeds the response limit; use ReadRange")
            maximum = MAXIMUM_FULL_READ_BYTES
        elif operation == 5:
            offset, maximum, expected = fields
            if (
                type(offset) is not int or offset < 0
                or type(maximum) is not int or not 0 < maximum <= MAXIMUM_RANGE_READ_BYTES
            ):
                raise ValueError("resource range exceeds the response limit")
        elif operation != 4:
            raise ValueError(f"unknown resource operation {operation}")
        observed = self._read(item, offset, maximum, expected)
        if isinstance(observed, list):
            return observed
        data, digest, token = observed
        if operation == 0:
            return variant(0, data, digest)
        if operation == 4:
            return variant(5, {0: item.content_size, 1: digest})
        return variant(6, data, offset, offset + len(data) >= item.content_size, token)

    def _read(
        self, item: ProjectFile, offset: int, maximum: int, expected: str | None = None
    ) -> tuple[bytes, str, str] | list[Any]:
        if item.payload is not None and item.payload[0] == 2:
            return variant(4, item.payload[1][0])
        if self.bundle.project_file is not None and (item.payload is None or item.payload[0] != 1):
            raise ValueError("packaged resource has no embedded binary payload")
        digest = blake3.blake3()
        data = bytearray()
        length = 0

        def consume(chunk: bytes) -> None:
            nonlocal length
            digest.update(chunk)
            start = max(0, offset - length)
            end = min(len(chunk), offset + maximum - length)
            if start < end:
                data.extend(chunk[start:end])
            length += len(chunk)

        if item.payload is not None and item.payload[0] == 1:
            raw = item.payload[1][0]
            token = f"resource:{item.content_hash.hex() if item.content_hash else ''}"
            if not isinstance(raw, bytes):
                raise ValueError("resource manifest has no binary payload")
            consume(raw)
        else:
            from .project import _source_signature, _validate_new_project_file

            source = item.source_path or self.bundle.root / PurePosixPath(item.relative_path)
            root = self.bundle.root.resolve(strict=True)
            try:
                resolved = source.resolve(strict=True)
            except RuntimeError as error:
                raise ValueError("resource path contains a symbolic-link loop") from error
            if resolved == root or root not in resolved.parents:
                raise PermissionError("resource path escaped the authorized root")
            _validate_new_project_file(root, source, item.category)
            before = _source_signature(source)
            if item.source_signature is not None and before != item.source_signature:
                return self._conflict()
            with source.open("rb") as stream:
                token = _change_token(source.stat())
                if expected is not None and expected != token:
                    return self._conflict()
                while chunk := stream.read(64 * 1024):
                    consume(chunk)
                    if length > item.content_size:
                        return self._conflict()
            if source.resolve(strict=True) != resolved or _source_signature(source) != before:
                return self._conflict()
        if (
            length != item.content_size or digest.digest() != item.content_hash
            or (expected is not None and token != expected)
        ):
            return self._conflict()
        return bytes(data), digest.hexdigest(), token

    @staticmethod
    def _conflict() -> list[Any]:
        return variant(4, {0: IO_CONFLICT, 1: "resource changed after project scan"})
