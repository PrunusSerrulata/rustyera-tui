"""Frontend-owned project scanning and storage I/O."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import blake3

from .frontend_io import (
    IO_ALREADY_EXISTS as IO_ALREADY_EXISTS,
    IO_CONFLICT as IO_CONFLICT,
    IO_INTERRUPTED as IO_INTERRUPTED,
    IO_INVALID_DATA as IO_INVALID_DATA,
    IO_NOT_FOUND as IO_NOT_FOUND,
    IO_OTHER as IO_OTHER,
    IO_PERMISSION_DENIED as IO_PERMISSION_DENIED,
    IO_READ_ONLY as IO_READ_ONLY,
    frontend_error,
)
from .storage import StorageBackend as StorageBackend
from .wire import variant

FILE_CSV = 0
FILE_ERH = 1
FILE_ERB = 2
FILE_RESOURCE_MANIFEST = 3
FILE_RESOURCE = 4
FILE_CONFIGURATION = 5

RESOURCE_IMAGE_SUFFIXES = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
RESOURCE_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
DEFAULT_MAXIMUM_ENVELOPE_BYTES = 128 * 1024 * 1024
DEFAULT_MAXIMUM_PAYLOAD_BYTES = 127 * 1024 * 1024
MAXIMUM_PROJECT_ENVELOPE_BYTES = 1024 * 1024 * 1024
PROJECT_ENVELOPE_HEADROOM_BYTES = 1024 * 1024
PROJECT_FILE_WIRE_OVERHEAD_BYTES = 256
ProjectScanProgress = Callable[[int, int], None]
ProjectConfigurationUpdate = Callable[[bytes, bytes, str], tuple[int, bytes]]


def _report_scan_progress(progress: ProjectScanProgress | None, completed: int, total: int) -> None:
    if progress is None:
        return
    if (
        total == 0
        or completed == total
        or completed * 100 // total > (completed - 1) * 100 // total
    ):
        progress(completed, total)


def _decode_project_source(raw: bytes, *, strict_utf8: bool = False) -> str:
    """Normalize a project source file to UTF-8 text at the frontend boundary."""

    if strict_utf8:
        return raw.decode("utf-8-sig")
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
    name = path.name.casefold()
    if name in {"reraconfig.toml", "setting.json"}:
        return FILE_CONFIGURATION
    suffix = path.suffix.casefold()
    return {
        ".csv": FILE_CSV,
        ".erh": FILE_ERH,
        ".erb": FILE_ERB,
        ".config": FILE_CONFIGURATION,
    }.get(suffix)


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


@dataclass(slots=True)
class ProjectBundle:
    root: Path
    revision: int
    files: dict[str, ProjectFile]
    project_file: Path | None = None
    quick_scan_pending: bool = False

    @classmethod
    def from_project_file_manifest(
        cls, project_file: Path, manifest: dict[int, Any]
    ) -> ProjectBundle:
        resolved = project_file.expanduser().resolve(strict=True)
        if resolved.suffix.casefold() != ".reraproj":
            raise ValueError("project file must use the .reraproj extension")
        revision = manifest.get(0)
        submitted = manifest.get(1)
        if not isinstance(revision, int) or not isinstance(submitted, list):
            raise ValueError("project file contains an invalid manifest")
        files: dict[str, ProjectFile] = {}
        for value in submitted:
            if not isinstance(value, dict):
                raise ValueError("project file contains an invalid file entry")
            relative = value.get(0)
            category = value.get(1)
            payload = value.get(2)
            content_hash = value.get(3)
            if (
                not isinstance(relative, str)
                or not isinstance(category, int)
                or not isinstance(payload, list)
                or (content_hash is not None and not isinstance(content_hash, bytes))
            ):
                raise ValueError("project file contains an invalid file entry")
            files[relative] = ProjectFile(
                relative_path=relative,
                category=category,
                payload=payload,
                content_hash=content_hash,
                content_size=_payload_size(payload),
            )
        return cls(resolved.parent, revision, files, resolved)

    @classmethod
    def scan(
        cls,
        root: Path,
        revision: int = 1,
        progress: ProjectScanProgress | None = None,
    ) -> ProjectBundle:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        if progress is not None:
            progress(0, 0)
        files: dict[str, ProjectFile] = {}
        paths = _project_paths(root)
        canonical_roots = _canonical_source_roots(root)
        candidates = [
            (path, category)
            for path in paths
            if (category := _classify_project_path(root, path, canonical_roots)) is not None
        ]
        if progress is not None:
            progress(0, len(candidates))
        for index, (path, category) in enumerate(candidates, 1):
            item = read_project_file(root, path, category)
            files[item.relative_path] = item
            _report_scan_progress(progress, index, len(candidates))
        return cls(root=root, revision=revision, files=files)

    @classmethod
    def scan_quick(
        cls,
        root: Path,
        revision: int = 1,
        progress: ProjectScanProgress | None = None,
    ) -> ProjectBundle:
        """Build a content identity using a persistent stat index without retaining source text."""

        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        if progress is not None:
            progress(0, 0)
        index_path = root / ".rustyera" / "cache" / "source-index-v1.json"
        index_current = False
        try:
            stored = json.loads(index_path.read_text(encoding="utf-8"))
            index_current = stored.get("version") == 1 and isinstance(stored.get("files"), dict)
            previous = stored["files"] if index_current else {}
        except (OSError, ValueError, TypeError):
            previous = {}
        files: dict[str, ProjectFile] = {}
        next_index: dict[str, Any] = {}
        canonical_roots = _canonical_source_roots(root)
        candidates = [
            (path, category)
            for path in _project_paths(root)
            if (category := _classify_project_path(root, path, canonical_roots)) is not None
        ]
        if progress is not None:
            progress(0, len(candidates))
        for index, (path, category) in enumerate(candidates, 1):
            relative = _normalize_relative_path(path.relative_to(root).as_posix())
            try:
                source_signature = _source_signature(path)
                signature = list(source_signature)
                prior = previous.get(relative)
                item: ProjectFile | None = None
                if (
                    prior
                    and prior.get("signature") == signature
                    and prior.get("category") == category
                    and isinstance(prior.get("size"), int)
                ):
                    digest = bytes.fromhex(prior["hash"])
                    content_size = prior["size"]
                else:
                    loaded = read_project_file(root, path, category)
                    item = ProjectFile(
                        loaded.relative_path,
                        loaded.category,
                        loaded.payload,
                        loaded.content_hash,
                        loaded.content_size,
                        loaded.source_path,
                        source_signature,
                    )
                    digest = item.content_hash
                    if digest is None:
                        raise ValueError(f"project file {relative} has no content hash")
                    content_size = item.content_size
                files[relative] = item or ProjectFile(
                    relative,
                    category,
                    None,
                    digest,
                    content_size,
                    source_path=path,
                    source_signature=source_signature,
                )
                next_index[relative] = {
                    "category": category,
                    "signature": signature,
                    "hash": digest.hex(),
                    "size": content_size,
                }
                _report_scan_progress(progress, index, len(candidates))
            except (OSError, UnicodeError, ValueError):
                # Error payloads and malformed index entries need the normal scanner's
                # precise diagnostic, so do not attempt a cache-only project load.
                return cls.scan(root, revision, progress)
        if not index_current or previous != next_index:
            _write_source_index(index_path, {"version": 1, "files": next_index})
        return cls(root=root, revision=revision, files=files, quick_scan_pending=True)

    @property
    def is_materialized(self) -> bool:
        return not self.quick_scan_pending and all(
            item.payload is not None for item in self.files.values()
        )

    def materialize(
        self,
        progress: ProjectScanProgress | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ProjectBundle:
        if self.is_materialized:
            return self
        if progress is not None:
            progress(0, len(self.files))
        files: dict[str, ProjectFile] = {}
        for index, item in enumerate(self.files.values(), 1):
            if cancelled is not None and cancelled():
                raise InterruptedError("full project export cancelled")
            materialized = item
            source_path = materialized.source_path or (
                self.root / PurePosixPath(materialized.relative_path)
            )
            try:
                signature_matches = materialized.source_signature == _source_signature(source_path)
            except OSError:
                signature_matches = False
            if materialized.payload is None or not signature_matches:
                materialized = read_project_file(self.root, source_path, materialized.category)
            files[materialized.relative_path] = materialized
            _report_scan_progress(progress, index, len(self.files))
        return ProjectBundle(self.root, self.revision, files, self.project_file)

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

    def requested_wire_limits(self) -> tuple[int, int]:
        """Return a conservative one-envelope project submission budget."""

        payload_bytes = sum(
            item.content_size
            + len(item.relative_path.encode("utf-8"))
            + PROJECT_FILE_WIRE_OVERHEAD_BYTES
            for item in self.files.values()
        )
        envelope_bytes = payload_bytes + PROJECT_ENVELOPE_HEADROOM_BYTES
        requested_payload = max(DEFAULT_MAXIMUM_PAYLOAD_BYTES, payload_bytes)
        requested_envelope = max(DEFAULT_MAXIMUM_ENVELOPE_BYTES, envelope_bytes)
        if requested_envelope > MAXIMUM_PROJECT_ENVELOPE_BYTES:
            raise ValueError(
                "project submission exceeds the frontend's 1 GiB envelope safety limit"
            )
        return requested_envelope, requested_payload

    def write_configuration(
        self,
        expected_digest: bytes,
        contents: str,
        prepare_project_update: ProjectConfigurationUpdate | None = None,
    ) -> None:
        """Persist the root config after an optimistic-lock check."""

        if self.project_file is not None:
            if prepare_project_update is None:
                raise PermissionError("当前 Runtime 不支持修改项目文件中的 reraconfig.toml")
            with self.project_file.open("r+b") as stream:
                project_bytes = stream.read()
                project_digest = blake3.blake3(project_bytes).digest()
                truncate_to, append = prepare_project_update(
                    project_bytes, expected_digest, contents
                )
                if not 0 <= truncate_to <= len(project_bytes):
                    raise RuntimeError("Runtime 返回了无效的项目配置更新位置")
                stream.seek(0)
                current_bytes = stream.read()
                if blake3.blake3(current_bytes).digest() != project_digest:
                    raise RuntimeError("项目文件已被其他程序修改，请重新打开偏好选项")
                stream.truncate(truncate_to)
                stream.seek(truncate_to)
                if stream.write(append) != len(append):
                    raise OSError("项目配置更新未能完整写入")
                stream.flush()
                os.fsync(stream.fileno())
            return
        target = next(
            (
                self.root / PurePosixPath(item.relative_path)
                for item in self.files.values()
                if item.category == FILE_CONFIGURATION
                and "/" not in item.relative_path
                and item.relative_path.casefold() == "reraconfig.toml"
            ),
            self.root / "reraconfig.toml",
        )
        try:
            text = _decode_project_source(target.read_bytes(), strict_utf8=True)
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            current = blake3.blake3(normalized.encode()).digest()
        except FileNotFoundError:
            current = b""
        requested = contents.replace("\r\n", "\n").replace("\r", "\n")
        requested_digest = blake3.blake3(requested.encode()).digest()
        if current == requested_digest:
            return
        if current != expected_digest:
            raise RuntimeError("reraconfig.toml 已被其他程序修改，请重新打开偏好选项")
        _atomic_write_text(target, contents)

    def resource_bytes(self, resource_id: str, content_digest: bytes) -> bytes:
        item = self.files.get(resource_id)
        if item is None:
            item = next(
                (
                    candidate
                    for path, candidate in self.files.items()
                    if path.casefold() == resource_id.casefold()
                ),
                None,
            )
        if item is None or item.category != FILE_RESOURCE:
            raise ValueError(f"unknown image resource {resource_id}")
        if item.payload is None:
            source_path = item.source_path
            if source_path is None:
                pure = PurePosixPath(item.relative_path)
                source_path = self.root.joinpath(*pure.parts)
            data = source_path.read_bytes()
            if item.content_hash is None or blake3.blake3(data).digest() != item.content_hash:
                raise ValueError(f"image resource {resource_id} changed after project scan")
        else:
            tag, fields = item.payload
            if tag != 1 or len(fields) != 1 or not isinstance(fields[0], bytes):
                raise ValueError(f"image resource {resource_id} has no binary payload")
            data = fields[0]
        if blake3.blake3(data).digest() != content_digest:
            raise ValueError(f"image resource {resource_id} digest does not match the project")
        return data

    def rescan(
        self, progress: ProjectScanProgress | None = None
    ) -> tuple[ProjectBundle, dict[int, Any]]:
        if self.project_file is not None:
            raise RuntimeError("a packaged project cannot reload source files")
        candidate = ProjectBundle.scan(self.root, self.revision + 1, progress)
        if self.quick_scan_pending:
            changes = [
                variant(0, item.submitted())
                for item in sorted(
                    candidate.files.values(), key=lambda value: value.relative_path.casefold()
                )
            ]
            return candidate, {0: self.revision, 1: candidate.revision, 2: changes}
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
        if self.project_file is not None:
            raise RuntimeError("a packaged project cannot reload source files")
        if self.quick_scan_pending:
            return self.rescan()
        expanded = path.expanduser()
        absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
        lexical = Path(os.path.abspath(absolute))
        if not lexical.is_file():
            raise FileNotFoundError(lexical)
        try:
            relative = _normalize_relative_path(lexical.relative_to(self.root).as_posix())
        except ValueError as error:
            raise ValueError("the script file must be inside the active project") from error
        category = _classify_project_path(self.root, lexical, _canonical_source_roots(self.root))
        if category not in (FILE_CSV, FILE_ERH, FILE_ERB, FILE_CONFIGURATION):
            raise ValueError("only project source and configuration files can be reloaded")
        item = read_project_file(self.root, lexical, category)
        candidate = ProjectBundle(self.root, self.revision + 1, dict(self.files))
        candidate.files[relative] = item
        return candidate, {
            0: self.revision,
            1: candidate.revision,
            2: [variant(0, item.submitted())],
        }


def _payload_size(payload: list[Any]) -> int:
    if len(payload) != 2 or not isinstance(payload[1], list) or len(payload[1]) != 1:
        return 0
    value = payload[1][0]
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return len(value)
    return 0


def read_project_file(root: Path, path: Path, category: int) -> ProjectFile:
    relative = _normalize_relative_path(path.relative_to(root).as_posix())
    try:
        raw = path.read_bytes()
        if category == FILE_RESOURCE:
            return ProjectFile(
                relative,
                category,
                variant(1, raw),
                blake3.blake3(raw).digest(),
                len(raw),
                path,
            )
        text = _decode_project_source(raw, strict_utf8=relative.casefold() == "reraconfig.toml")
        if category == FILE_RESOURCE_MANIFEST:
            text = _normalize_resource_manifest_paths(text)
        normalized = text.encode("utf-8")
        return ProjectFile(
            relative,
            category,
            variant(0, text),
            blake3.blake3(normalized).digest(),
            len(normalized),
            path,
        )
    except (OSError, UnicodeError) as error:
        return ProjectFile(relative, category, variant(2, frontend_error(error)), None)


def _normalize_relative_path(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def _normalize_resource_manifest_paths(text: str) -> str:
    normalized: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        fields = body.split(",")
        if len(fields) >= 2 and fields[1].strip() and fields[1].strip().casefold() != "anime":
            value = fields[1]
            leading = value[: len(value) - len(value.lstrip())]
            trailing = value[len(value.rstrip()) :]
            fields[1] = f"{leading}{unicodedata.normalize('NFC', value.strip())}{trailing}"
        normalized.append(",".join(fields) + ending)
    return "".join(normalized)


def _normalized_project_bytes(raw: bytes, category: int) -> bytes:
    if category == FILE_RESOURCE:
        return raw
    text = _decode_project_source(raw)
    if category == FILE_RESOURCE_MANIFEST:
        text = _normalize_resource_manifest_paths(text)
    return text.encode("utf-8")


def _source_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        getattr(stat, "st_dev", 0),
        getattr(stat, "st_ino", 0),
    )


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


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        native_contents = contents.replace("\r\n", "\n").replace("\r", "\n")
        native_contents = native_contents.replace("\n", os.linesep)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(native_contents)
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
    parts = path.relative_to(root).parts
    first = parts[0].casefold()
    if first == "resources":
        if path.suffix.casefold() == ".csv":
            return FILE_RESOURCE_MANIFEST
        if path.suffix.casefold() in RESOURCE_IMAGE_SUFFIXES | RESOURCE_AUDIO_SUFFIXES:
            return FILE_RESOURCE
        return None
    category = classify_path(path)
    if category is None:
        return None
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
