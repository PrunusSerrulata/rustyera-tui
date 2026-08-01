"""Frontend-owned runtime storage namespace and filesystem operations."""

from __future__ import annotations

import fnmatch
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import blake3

from .frontend_io import IO_CONFLICT, IO_INVALID_DATA, frontend_error
from .wire import variant


class StorageBackend:
    """Resolve runtime storage requests without exposing paths to the runtime.

    Revisions are content digests. Atomic replacement and optimistic preconditions are applied
    by the frontend because it is the component that owns filesystem race semantics.
    """

    def __init__(self, project_root: Path, data_root: Path | None = None):
        self.project_root = project_root.resolve()
        configured = os.environ.get("ERA_TUI_DATA_DIR")
        if configured or data_root is not None:
            base = Path(configured).expanduser() if configured else data_root
            assert base is not None
            project_key = blake3.blake3(self.project_root.as_posix().encode("utf-8")).hexdigest()[
                :16
            ]
            self.data_root = base.resolve() / "games" / project_key
        else:
            self.data_root = self.project_root
        self.idempotent_results: dict[str, list[Any]] = {}

    def compiled_cache_path(self) -> Path:
        """Return the frontend-private opaque compiler cache path for this project."""

        return self.data_root / ".rustyera" / "cache" / "compiled-project-v8.bin.zst"

    def obsolete_compiled_cache_paths(self) -> tuple[Path, ...]:
        root = self.data_root / ".rustyera" / "cache"
        return (
            root / "compiled-project-v7.bin.zst",
            root / "compiled-project-v6.bin.zst",
            root / "compiled-project-v5.bin.zst",
            root / "compiled-project-v4.bin.zst",
            root / "compiled-project-v3.bin.zst",
            root / "compiled-project-v2.bin.zst",
            root / "compiled-project-v1.bin.gz",
        )

    def _namespace_root(self, namespace: int) -> Path:
        roots = {
            0: self.data_root / "project",
            1: self.data_root / "sav",
            2: self.data_root / "sav",
            3: self.data_root / "data",
            4: self.data_root / "logs",
            5: self.project_root,
        }
        return roots.get(namespace, self.project_root)

    def _resolve_for_read(self, namespace: int, relative: str) -> tuple[Path, Path]:
        root = self._namespace_root(namespace).resolve()
        primary = self._resolve(namespace, relative)
        if namespace in (0, 3) and not primary.exists():
            pure = PurePosixPath(relative)
            fallback = self.project_root.joinpath(*pure.parts).resolve()
            if fallback == self.project_root or self.project_root in fallback.parents:
                return self.project_root, fallback
        return root, primary

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
            result = variant(4, frontend_error(error, IO_INVALID_DATA))
        except OSError as error:
            result = variant(4, frontend_error(error))
        if idempotency_key and operation_tag in (1, 3):
            self.idempotent_results[idempotency_key] = result
        return {0: request_id, 1: result}

    def _operate(
        self, namespace: int, relative: str, operation_tag: int, fields: list[Any]
    ) -> list[Any]:
        if operation_tag in (0, 2, 4, 5):
            read_root, path = self._resolve_for_read(namespace, relative)
        else:
            read_root = self._namespace_root(namespace).resolve()
            path = self._resolve(namespace, relative)
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
            search_root = path if relative else read_root
            candidates: Iterable[Path]
            candidates = search_root.rglob("*") if recursive else search_root.glob("*")
            entries = []
            for candidate in sorted((item for item in candidates if item.is_file())):
                candidate_relative = candidate.relative_to(read_root).as_posix()
                if pattern and not fnmatch.fnmatch(PurePosixPath(candidate_relative).name, pattern):
                    continue
                stat = candidate.stat()
                entries.append(
                    {
                        0: candidate_relative,
                        1: stat.st_size,
                        2: None,
                        3: self._change_token(stat),
                    }
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
        if operation_tag == 5:  # ReadRange
            offset, maximum_bytes, expected_token = fields
            before = path.stat()
            token = self._change_token(before)
            if expected_token is not None and expected_token != token:
                return variant(4, {0: IO_CONFLICT, 1: "storage file changed during metadata read"})
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(maximum_bytes)
            after = path.stat()
            after_token = self._change_token(after)
            if token != after_token:
                return variant(4, {0: IO_CONFLICT, 1: "storage file changed during metadata read"})
            complete = offset + len(data) >= after.st_size
            return variant(6, data, offset, complete, token)
        raise ValueError(f"unknown storage operation {operation_tag}")

    @staticmethod
    def _change_token(stat: os.stat_result) -> str:
        return ":".join(
            str(value)
            for value in (
                getattr(stat, "st_dev", 0),
                getattr(stat, "st_ino", 0),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        )

    def _precondition_conflict(self, path: Path, precondition: list[Any]) -> list[Any] | None:
        tag, fields = precondition
        revision = self._revision(path)
        conflict = tag == 1 and revision is not None
        conflict = conflict or (tag == 2 and (not fields or revision != fields[0]))
        if not conflict:
            return None
        error = {0: IO_CONFLICT, 1: "storage precondition did not hold"}
        return variant(4, error)
