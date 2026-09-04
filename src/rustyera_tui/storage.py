"""Frontend-owned runtime storage namespace and filesystem operations."""

from __future__ import annotations

import os
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, Iterator

import blake3

from .frontend_io import IO_CONFLICT, IO_INVALID_DATA, frontend_error
from .storage_state import _change_token, _precondition_conflict
from .storage_listing import list_storage
from .storage_path import ResolvedDataPath, normalized_data_path, resolve_data_path
from .text_budget import utf8_length
from .wire import encode, variant
from .resource_storage import ResourceStorage

if TYPE_CHECKING:
    from .project_bundle import ProjectBundle

MAXIMUM_STORAGE_RESPONSE_BYTES = 64 * 1024 * 1024


def _file_digest(path: Path) -> str:
    hasher = blake3.blake3()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_path_exists(requested: Path, canonical: Path) -> bool:
    try:
        requested.lstat()
    except FileNotFoundError:
        return False
    try:
        canonical.stat()
    except FileNotFoundError as error:
        raise ValueError("storage path changed or contains a dangling link") from error
    return True


class StorageBackend:
    """Resolve runtime storage requests without exposing paths to the runtime.

    Revisions are content digests. Atomic replacement and optimistic preconditions are applied
    by the frontend because it is the component that owns filesystem race semantics.
    """

    def __init__(
        self,
        project_root: Path,
        data_root: Path | None = None,
        identity_path: Path | None = None,
        compatibility_profile: str = "emuera.em",
        resource_bundle: ProjectBundle | None = None,
    ):
        self.project_root = project_root.resolve()
        configured = os.environ.get("ERA_TUI_DATA_DIR")
        if configured or data_root is not None:
            base = Path(configured).expanduser() if configured else data_root
            assert base is not None
            identity = (identity_path or self.project_root).resolve().as_posix()
            project_key = blake3.blake3(identity.encode("utf-8")).hexdigest()[:16]
            self.data_root = base.resolve() / "games" / project_key
        else:
            self.data_root = self.project_root
        self.save_root = self.data_root
        if compatibility_profile == "emuera.skia.snake":
            # Directory projects exchange standard Emuera saves in their own sav directory.
            # Packaged projects instead use the persistent project copy selected by the caller.
            self.save_root = self.data_root if identity_path is not None else self.project_root
            self.data_root = self.data_root / ".rustyera" / "profiles" / compatibility_profile
        elif compatibility_profile != "emuera.em":
            raise ValueError("unsupported project compatibility profile")
        self.compatibility_profile = compatibility_profile
        self.resources = ResourceStorage(resource_bundle) if resource_bundle is not None else None
        self.idempotent_results: OrderedDict[str, list[Any]] = OrderedDict()
        self._idempotent_result_sizes: dict[str, int] = {}
        self.idempotent_result_bytes = 0
        self.idempotency_epoch: int | None = None
        self.maximum_idempotent_results = 1_024
        self.maximum_idempotent_bytes = 4 * 1024 * 1024

    def begin_epoch(self, epoch: int | None) -> None:
        """Discard command results that cannot be replayed in the next VM epoch."""

        if epoch != self.idempotency_epoch:
            self.idempotent_results.clear()
            self._idempotent_result_sizes.clear()
            self.idempotent_result_bytes = 0
            self.idempotency_epoch = epoch

    def bind_resources(self, bundle: ProjectBundle) -> None:
        """Bind only the runtime client's committed bundle, never its reload candidate."""
        if self.resources is None or self.resources.bundle is not bundle:
            self.resources = ResourceStorage(bundle)

    def compiled_cache_path(self) -> Path:
        """Return the frontend-private opaque compiler cache path for this project."""

        return self.data_root / ".rustyera" / "cache" / "compiled-project.reracache"

    def obsolete_compiled_cache_paths(self) -> tuple[Path, ...]:
        root = self.data_root / ".rustyera" / "cache"
        return (
            root / "compiled-project.reraproj",
            root / "compiled-project-v8.bin.zst",
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
            1: self.save_root / "sav",
            2: self.save_root / "sav",
            3: self.data_root / "data",
            4: self.data_root / "logs",
            5: self.project_root,
        }
        return roots.get(namespace, self.project_root)

    def _resolve_for_read(self, namespace: int, relative: str) -> tuple[Path, ResolvedDataPath]:
        namespace_root = self._namespace_root(namespace)
        if self.compatibility_profile == "emuera.skia.snake" and namespace == 3:
            selected = resolve_data_path(namespace_root, relative)
            return namespace_root.resolve(), selected
        root = namespace_root.resolve()
        primary = self._resolve(namespace, relative)
        pure = PurePosixPath(relative)
        exists = _read_path_exists(namespace_root.joinpath(*pure.parts), primary)
        if self.compatibility_profile == "emuera.em" and namespace in (0, 3) and not exists:
            requested = self.project_root.joinpath(*pure.parts)
            fallback = requested.resolve()
            if fallback == self.project_root or self.project_root in fallback.parents:
                logical = (
                    fallback.relative_to(self.project_root).as_posix()
                    if fallback != self.project_root
                    else ""
                )
                return self.project_root, ResolvedDataPath(
                    fallback,
                    logical,
                    _read_path_exists(requested, fallback),
                )
        logical = primary.relative_to(root).as_posix() if primary != root else ""
        return root, ResolvedDataPath(primary, logical, exists)

    def _resolve(self, namespace: int, relative: str) -> Path:
        if self.compatibility_profile == "emuera.skia.snake" and namespace == 3:
            return normalized_data_path(self._namespace_root(namespace), relative)
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

    def handle(self, request: dict[int, Any]) -> dict[int, Any]:
        request_id = request[0]
        namespace = request[1]
        relative = request[2]
        operation_tag, fields = request[3]
        idempotency_key = request.get(4, "")
        if namespace != 5 and idempotency_key and idempotency_key in self.idempotent_results:
            result = self.idempotent_results[idempotency_key]
            self.idempotent_results.move_to_end(idempotency_key)
            return {0: request_id, 1: result}
        try:
            result = self._operate(namespace, relative, operation_tag, fields)
        except ValueError as error:
            result = variant(4, frontend_error(error, IO_INVALID_DATA))
        except OSError as error:
            result = variant(4, frontend_error(error))
        if namespace != 5 and idempotency_key and operation_tag in (1, 3):
            retained_bytes = utf8_length(idempotency_key) + len(encode(result))
            if retained_bytes <= self.maximum_idempotent_bytes:
                previous_size = self._idempotent_result_sizes.get(idempotency_key, 0)
                self.idempotent_result_bytes -= previous_size
                self.idempotent_results[idempotency_key] = result
                self._idempotent_result_sizes[idempotency_key] = retained_bytes
                self.idempotent_result_bytes += retained_bytes
                self.idempotent_results.move_to_end(idempotency_key)
                while (
                    len(self.idempotent_results) > self.maximum_idempotent_results
                    or self.idempotent_result_bytes > self.maximum_idempotent_bytes
                ):
                    evicted_key, _ = self.idempotent_results.popitem(last=False)
                    self.idempotent_result_bytes -= self._idempotent_result_sizes.pop(evicted_key)
        return {0: request_id, 1: result}

    def _operate(
        self, namespace: int, relative: str, operation_tag: int, fields: list[Any]
    ) -> list[Any]:
        if namespace == 5:
            if operation_tag in (1, 3):
                return variant(4, {0: 4, 1: "Resource storage is read-only"})
            if self.resources is None:
                raise PermissionError("no active project resource manifest")
            return self.resources.operate(relative, operation_tag, fields)
        if operation_tag in (0, 2, 4, 5):
            read_root, selected = self._resolve_for_read(namespace, relative)
            path = selected.canonical
        else:
            read_root = self._namespace_root(namespace).resolve()
            path = self._resolve(namespace, relative)
        if operation_tag == 0:  # Read
            with path.open("rb") as stream:
                data = stream.read(MAXIMUM_STORAGE_RESPONSE_BYTES + 1)
            if len(data) > MAXIMUM_STORAGE_RESPONSE_BYTES:
                raise ValueError("storage file exceeds the frontend response limit; use ReadRange")
            return variant(0, data, blake3.blake3(data).hexdigest())
        if operation_tag == 1:  # Write
            data, atomic_replace, precondition = fields
            with self._mutation_lock(namespace):
                conflict = _precondition_conflict(path, precondition)
                if conflict is not None:
                    return conflict
                path.parent.mkdir(parents=True, exist_ok=True)
                if atomic_replace:
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=f".{path.name}.", dir=path.parent
                    )
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
            entries = list_storage(
                read_root,
                selected,
                pattern,
                recursive,
                self.compatibility_profile == "emuera.skia.snake" and namespace == 3,
            )
            return variant(2, entries)
        if operation_tag == 3:  # Delete
            with self._mutation_lock(namespace):
                conflict = _precondition_conflict(path, fields[0])
                if conflict is not None:
                    return conflict
                path.unlink()
                return variant(3)
        if operation_tag == 4:  # Stat
            before = path.stat()
            digest = _file_digest(path)
            after = path.stat()
            if _change_token(before) != _change_token(after):
                return variant(4, {0: IO_CONFLICT, 1: "storage file changed during stat"})
            return variant(5, {0: after.st_size, 1: digest})
        if operation_tag == 5:  # ReadRange
            # The wire codec omits a trailing None change token on the first chunk.
            offset, maximum_bytes, expected_token = [*fields, None] if len(fields) == 2 else fields
            if (
                not isinstance(offset, int)
                or offset < 0
                or not isinstance(maximum_bytes, int)
                or not 0 <= maximum_bytes <= MAXIMUM_STORAGE_RESPONSE_BYTES
            ):
                raise ValueError("storage range exceeds the frontend response limit")
            before = path.stat()
            token = _change_token(before)
            if expected_token is not None and expected_token != token:
                return variant(4, {0: IO_CONFLICT, 1: "storage file changed during metadata read"})
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(maximum_bytes)
            after = path.stat()
            after_token = _change_token(after)
            if token != after_token:
                return variant(4, {0: IO_CONFLICT, 1: "storage file changed during metadata read"})
            complete = offset + len(data) >= after.st_size
            return variant(6, data, offset, complete, token)
        raise ValueError(f"unknown storage operation {operation_tag}")

    @contextmanager
    def _mutation_lock(self, namespace: int) -> Iterator[None]:
        """Serialize Data CAS precondition checks and replacements across frontend processes."""

        if namespace != 3:
            yield
            return
        lock_path = self.data_root / ".rustyera" / "locks" / "data-storage.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            _lock_stream(stream)
            try:
                yield
            finally:
                _unlock_stream(stream)


def _lock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
