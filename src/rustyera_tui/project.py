"""Frontend-owned project scanning and storage I/O."""

from __future__ import annotations

import fnmatch
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import blake3
from platformdirs import user_data_path

from .wire import variant

FILE_CSV = 0
FILE_ERH = 1
FILE_ERB = 2
FILE_RESOURCE_MANIFEST = 3
FILE_RESOURCE = 4
FILE_CONFIGURATION = 5

IO_NOT_FOUND = 0
IO_PERMISSION_DENIED = 1
IO_INVALID_DATA = 2
IO_INTERRUPTED = 3
IO_READ_ONLY = 4
IO_ALREADY_EXISTS = 5
IO_OTHER = 6
IO_CONFLICT = 7


def classify_path(path: Path | PurePosixPath) -> int | None:
    suffix = path.suffix.casefold()
    return {
        ".csv": FILE_CSV,
        ".erh": FILE_ERH,
        ".erb": FILE_ERB,
        ".config": FILE_CONFIGURATION,
    }.get(suffix)


def _frontend_error(error: OSError | UnicodeError, kind: int | None = None) -> dict[int, Any]:
    if kind is None:
        if isinstance(error, FileNotFoundError):
            kind = IO_NOT_FOUND
        elif isinstance(error, PermissionError):
            kind = IO_PERMISSION_DENIED
        elif isinstance(error, UnicodeError):
            kind = IO_INVALID_DATA
        elif isinstance(error, InterruptedError):
            kind = IO_INTERRUPTED
        elif isinstance(error, FileExistsError):
            kind = IO_ALREADY_EXISTS
        else:
            kind = IO_OTHER
    platform_code = error.errno if isinstance(error, OSError) else None
    result: dict[int, Any] = {0: kind, 1: str(error)}
    if platform_code is not None:
        result[2] = platform_code
    return result


@dataclass(frozen=True, slots=True)
class ProjectFile:
    relative_path: str
    category: int
    payload: list[Any]
    content_hash: bytes | None

    def submitted(self) -> dict[int, Any]:
        result: dict[int, Any] = {
            0: self.relative_path,
            1: self.category,
            2: self.payload,
        }
        if self.content_hash is not None:
            result[3] = self.content_hash
        return result


@dataclass(slots=True)
class ProjectBundle:
    root: Path
    revision: int
    files: dict[str, ProjectFile]

    @classmethod
    def scan(cls, root: Path, revision: int = 1) -> ProjectBundle:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        files: dict[str, ProjectFile] = {}
        paths = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )
        for path in paths:
            category = classify_path(path)
            if category is None:
                continue
            item = read_project_file(root, path, category)
            files[item.relative_path] = item
        return cls(root=root, revision=revision, files=files)

    def manifest(self) -> dict[int, Any]:
        ordered = sorted(self.files.values(), key=lambda item: item.relative_path.casefold())
        return {0: self.revision, 1: [item.submitted() for item in ordered]}

    def rescan(self) -> tuple[ProjectBundle, dict[int, Any]]:
        candidate = ProjectBundle.scan(self.root, self.revision + 1)
        changes: list[Any] = []
        for relative_path in sorted(set(self.files) | set(candidate.files), key=str.casefold):
            old = self.files.get(relative_path)
            new = candidate.files.get(relative_path)
            if new is None and old is not None:
                changes.append(variant(1, old.category, relative_path))
            elif new is not None and new != old:
                changes.append(variant(0, new.submitted()))
        reload_request = {0: self.revision, 1: candidate.revision, 2: changes}
        return candidate, reload_request

    def reload_file(self, path: Path) -> tuple[ProjectBundle, dict[int, Any]]:
        resolved = path.expanduser().resolve(strict=True)
        try:
            relative = resolved.relative_to(self.root).as_posix()
        except ValueError as error:
            raise ValueError("the script file must be inside the active project") from error
        category = classify_path(resolved)
        if category not in (FILE_CSV, FILE_ERH, FILE_ERB, FILE_CONFIGURATION):
            raise ValueError("only .csv, .erh, .erb, and .config files can be reloaded")
        item = read_project_file(self.root, resolved, category)
        candidate = ProjectBundle(self.root, self.revision + 1, dict(self.files))
        candidate.files[relative] = item
        return candidate, {
            0: self.revision,
            1: candidate.revision,
            2: [variant(0, item.submitted())],
        }


def read_project_file(root: Path, path: Path, category: int) -> ProjectFile:
    relative = path.relative_to(root).as_posix()
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        return ProjectFile(
            relative, category, variant(0, text), blake3.blake3(text.encode("utf-8")).digest()
        )
    except (OSError, UnicodeError) as error:
        return ProjectFile(relative, category, variant(2, _frontend_error(error)), None)


class StorageBackend:
    """Resolve runtime storage requests without exposing paths to the runtime.

    Revisions are content digests. Atomic replacement and optimistic preconditions are applied
    by the frontend because it is the component that owns filesystem race semantics.
    """

    def __init__(self, project_root: Path, data_root: Path | None = None):
        self.project_root = project_root.resolve()
        configured = os.environ.get("ERA_TUI_DATA_DIR")
        base = (
            Path(configured).expanduser()
            if configured
            else (data_root or user_data_path("RustyEra", "RustyEra"))
        )
        project_key = blake3.blake3(self.project_root.as_posix().encode("utf-8")).hexdigest()[:16]
        self.data_root = base.resolve() / "games" / project_key
        self.idempotent_results: dict[str, list[Any]] = {}

    def _namespace_root(self, namespace: int) -> Path:
        roots = {
            0: self.data_root / "project",
            1: self.data_root / "save",
            2: self.data_root / "global",
            3: self.data_root / "data",
            4: self.data_root / "logs",
            5: self.project_root,
        }
        return roots.get(namespace, self.project_root)

    def _resolve_for_read(self, namespace: int, relative: str) -> Path:
        primary = self._resolve(namespace, relative)
        if namespace == 0 and not primary.exists():
            pure = PurePosixPath(relative)
            fallback = self.project_root.joinpath(*pure.parts).resolve()
            if fallback == self.project_root or self.project_root in fallback.parents:
                return fallback
        return primary

    def _resolve(self, namespace: int, relative: str) -> Path:
        if not relative:
            return self._namespace_root(namespace).resolve()
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ValueError("storage path is not a safe relative path")
        root = self._namespace_root(namespace).resolve()
        resolved = root.joinpath(*pure.parts).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("storage path escapes its namespace")
        return resolved

    @staticmethod
    def _revision(path: Path) -> str | None:
        try:
            return blake3.blake3(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return None

    def handle(self, request: dict[int, Any]) -> dict[int, Any]:
        request_id = request[0]
        namespace = request[1]
        relative = request[2]
        operation_tag, fields = request[3]
        idempotency_key = request.get(4, "")
        if idempotency_key and idempotency_key in self.idempotent_results:
            return {0: request_id, 1: self.idempotent_results[idempotency_key]}
        try:
            result = self._operate(namespace, relative, operation_tag, fields)
        except ValueError as error:
            result = variant(4, _frontend_error(error, IO_INVALID_DATA))
        except OSError as error:
            result = variant(4, _frontend_error(error))
        if idempotency_key and operation_tag in (1, 3):
            self.idempotent_results[idempotency_key] = result
        return {0: request_id, 1: result}

    def _operate(
        self, namespace: int, relative: str, operation_tag: int, fields: list[Any]
    ) -> list[Any]:
        path = (
            self._resolve_for_read(namespace, relative)
            if operation_tag in (0, 4)
            else self._resolve(namespace, relative)
        )
        if operation_tag == 0:  # Read
            data = path.read_bytes()
            return variant(0, data, blake3.blake3(data).hexdigest())
        if operation_tag == 1:  # Write
            data, atomic_replace, precondition = fields
            conflict = self._precondition_conflict(path, precondition)
            if conflict is not None:
                return conflict
            path.parent.mkdir(parents=True, exist_ok=True)
            if atomic_replace:
                descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            else:
                path.write_bytes(data)
            return variant(1, blake3.blake3(data).hexdigest())
        if operation_tag == 2:  # List
            pattern, recursive = fields
            root = self._namespace_root(namespace).resolve()
            search_root = path if relative else root
            candidates: Iterable[Path]
            candidates = search_root.rglob("*") if recursive else search_root.glob("*")
            entries = []
            for candidate in sorted((item for item in candidates if item.is_file())):
                candidate_relative = candidate.relative_to(root).as_posix()
                if pattern and not fnmatch.fnmatch(PurePosixPath(candidate_relative).name, pattern):
                    continue
                data = candidate.read_bytes()
                entries.append(
                    {0: candidate_relative, 1: len(data), 2: blake3.blake3(data).hexdigest()}
                )
            return variant(2, entries)
        if operation_tag == 3:  # Delete
            conflict = self._precondition_conflict(path, fields[0])
            if conflict is not None:
                return conflict
            path.unlink()
            return variant(3)
        if operation_tag == 4:  # Stat
            data = path.read_bytes()
            return variant(5, {0: len(data), 1: blake3.blake3(data).hexdigest()})
        raise ValueError(f"unknown storage operation {operation_tag}")

    def _precondition_conflict(self, path: Path, precondition: list[Any]) -> list[Any] | None:
        tag, fields = precondition
        revision = self._revision(path)
        conflict = tag == 1 and revision is not None
        conflict = conflict or (tag == 2 and (not fields or revision != fields[0]))
        if not conflict:
            return None
        error = {0: IO_CONFLICT, 1: "storage precondition did not hold"}
        return variant(4, error)
