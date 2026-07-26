"""Frontend-owned project scanning and storage I/O."""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import blake3

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


def _decode_project_source(raw: bytes) -> str:
    """Normalize a project source file to UTF-8 text at the frontend boundary."""

    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # The reference loader makes the same strict UTF-8-first choice and treats
        # an invalid stream as Windows-31J. Runtime-facing text remains UTF-8.
        try:
            return raw.decode("cp932")
        except UnicodeDecodeError:
            # Some translated projects contain an isolated GBK source among otherwise
            # UTF-8 or Windows-31J files. Normalize that legacy file at this I/O boundary.
            return raw.decode("gbk")


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
    payload: list[Any] | None
    content_hash: bytes | None

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
        paths = _project_paths(root)
        canonical_roots = _canonical_source_roots(root)
        for path in paths:
            category = _classify_project_path(root, path, canonical_roots)
            if category is None:
                continue
            item = read_project_file(root, path, category)
            files[item.relative_path] = item
        return cls(root=root, revision=revision, files=files)

    @classmethod
    def scan_quick(cls, root: Path, revision: int = 1) -> ProjectBundle:
        """Build a content identity using a persistent stat index without retaining source text."""

        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        index_path = root / ".rustyera" / "cache" / "source-index-v1.json"
        try:
            stored = json.loads(index_path.read_text(encoding="utf-8"))
            previous = stored.get("files", {}) if stored.get("version") == 1 else {}
        except (OSError, ValueError, TypeError):
            previous = {}
        files: dict[str, ProjectFile] = {}
        next_index: dict[str, Any] = {}
        canonical_roots = _canonical_source_roots(root)
        for path in _project_paths(root):
            category = _classify_project_path(root, path, canonical_roots)
            if category is None:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                signature = [
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    getattr(stat, "st_dev", 0),
                    getattr(stat, "st_ino", 0),
                ]
                prior = previous.get(relative)
                if (
                    prior
                    and prior.get("signature") == signature
                    and prior.get("category") == category
                ):
                    digest = bytes.fromhex(prior["hash"])
                else:
                    raw = path.read_bytes()
                    text = _decode_project_source(raw)
                    digest = blake3.blake3(text.encode("utf-8")).digest()
                files[relative] = ProjectFile(relative, category, None, digest)
                next_index[relative] = {
                    "category": category,
                    "signature": signature,
                    "hash": digest.hex(),
                }
            except (OSError, UnicodeError, ValueError):
                # Error payloads and malformed index entries need the normal scanner's
                # precise diagnostic, so do not attempt a cache-only project load.
                return cls.scan(root, revision)
        _write_source_index(index_path, {"version": 1, "files": next_index})
        return cls(root=root, revision=revision, files=files)

    @property
    def is_materialized(self) -> bool:
        return all(item.payload is not None for item in self.files.values())

    def materialize(self) -> ProjectBundle:
        return self if self.is_materialized else ProjectBundle.scan(self.root, self.revision)

    def identity(self) -> dict[int, Any]:
        hasher = blake3.blake3(derive_key_context="rustyera.project-source-identity.v1")
        ordered = sorted(
            self.files.values(), key=lambda item: (item.relative_path.lower(), item.relative_path)
        )
        for item in ordered:
            digest = item.content_hash
            if digest is None and item.payload is not None and item.payload[0] == 2:
                digest = blake3.blake3(str(item.payload[1][0][1]).encode("utf-8")).digest()
            if digest is None:
                raise RuntimeError(f"project file {item.relative_path} has no content hash")
            path = item.relative_path.encode("utf-8")
            hasher.update(len(path).to_bytes(8, "little"))
            hasher.update(path)
            hasher.update(bytes([item.category]))
            hasher.update(digest)
        return {0: self.revision, 1: hasher.digest()}

    def manifest(self) -> dict[int, Any]:
        if not self.is_materialized:
            raise RuntimeError("project source payloads have not been materialized")
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
            elif new is not None and (
                old is None or new.category != old.category or new.content_hash != old.content_hash
            ):
                changes.append(variant(0, new.submitted()))
        reload_request = {0: self.revision, 1: candidate.revision, 2: changes}
        return candidate, reload_request

    def reload_file(self, path: Path) -> tuple[ProjectBundle, dict[int, Any]]:
        expanded = path.expanduser()
        absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
        lexical = Path(os.path.abspath(absolute))
        if not lexical.is_file():
            raise FileNotFoundError(lexical)
        try:
            relative = lexical.relative_to(self.root).as_posix()
        except ValueError as error:
            raise ValueError("the script file must be inside the active project") from error
        category = _classify_project_path(self.root, lexical, _canonical_source_roots(self.root))
        if category not in (FILE_CSV, FILE_ERH, FILE_ERB, FILE_CONFIGURATION):
            raise ValueError("only .csv, .erh, .erb, and .config files can be reloaded")
        item = read_project_file(self.root, lexical, category)
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
        text = _decode_project_source(raw)
        return ProjectFile(
            relative, category, variant(0, text), blake3.blake3(text.encode("utf-8")).digest()
        )
    except (OSError, UnicodeError) as error:
        return ProjectFile(relative, category, variant(2, _frontend_error(error)), None)


def _write_source_index(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _canonical_source_roots(root: Path) -> frozenset[str]:
    try:
        return frozenset(
            entry.name.casefold()
            for entry in root.iterdir()
            if entry.is_dir() and entry.name.casefold() in {"csv", "erb"}
        )
    except OSError:
        return frozenset()


def _classify_project_path(root: Path, path: Path, canonical_roots: frozenset[str]) -> int | None:
    category = classify_path(path)
    if category is None:
        return None
    parts = path.relative_to(root).parts
    first = parts[0].casefold()
    if category in (FILE_ERH, FILE_ERB) and "erb" in canonical_roots and first != "erb":
        return None
    if category == FILE_CSV and "csv" in canonical_roots and first != "csv":
        return None
    if (
        category == FILE_CONFIGURATION
        and "csv" in canonical_roots
        and len(parts) > 1
        and first != "csv"
    ):
        return None
    return category


def _project_paths(root: Path) -> list[Path]:
    """Enumerate project files while following resource-directory links once."""

    paths: list[Path] = []
    root_stat = root.stat()
    visited = {(root_stat.st_dev, root_stat.st_ino)}
    for directory, names, filenames in os.walk(root, followlinks=True):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in sorted(names, key=str.casefold):
            if name == ".rustyera":
                continue
            try:
                stat = (directory_path / name).stat()
            except OSError:
                continue
            identity = (stat.st_dev, stat.st_ino)
            if identity in visited:
                continue
            visited.add(identity)
            retained.append(name)
        names[:] = retained
        paths.extend(directory_path / name for name in filenames)
    return sorted(
        paths,
        key=lambda path: (
            path.relative_to(root).as_posix().casefold(),
            path.relative_to(root).as_posix(),
        ),
    )


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

        return self.data_root / ".rustyera" / "cache" / "compiled-project-v7.bin.zst"

    def obsolete_compiled_cache_paths(self) -> tuple[Path, ...]:
        root = self.data_root / ".rustyera" / "cache"
        return (
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
            if operation_tag in (0, 4, 5)
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
