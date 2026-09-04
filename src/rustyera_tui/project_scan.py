"""Project scanning, stable reads, path classification, and source index helpers."""

from __future__ import annotations

import errno

from . import project as project_facade
from .project import (
    Any,
    Callable,
    FILE_ALS,
    FILE_CONFIGURATION,
    FILE_CSV,
    FILE_ERB,
    FILE_ERD,
    FILE_ERH,
    FILE_RESOURCE,
    FILE_RESOURCE_MANIFEST,
    FIRST_COMPLETED,
    Future,
    IO_CONFLICT,
    Input,
    Output,
    Path,
    ProjectFile,
    ProjectScanProgress,
    PurePosixPath,
    RESOURCE_AUDIO_SUFFIXES,
    RESOURCE_DATA_EXCLUDED_ROOTS,
    RESOURCE_DATA_SUFFIXES,
    RESOURCE_FONT_SUFFIXES,
    RESOURCE_IMAGE_SUFFIXES,
    STABLE_READ_ATTEMPTS,
    Sequence,
    ThreadPoolExecutor,
    _ProjectChangedDuringScan,
    blake3,
    cast,
    frontend_error,
    os,
    variant,
    wait,
)
from .project_source_index import (
    _normalize_relative_path,
    _normalize_resource_manifest_paths,
    _path_sort_key,
)
from .text_budget import iter_utf8_chunks, utf8_length


def _read_error_file(
    root: Path, path: Path, category: int, error: OSError | UnicodeError
) -> ProjectFile:
    relative = _normalize_relative_path(path.relative_to(root).as_posix())
    return ProjectFile(relative, category, variant(2, frontend_error(error)), None)


def _stable_read_project_file(root: Path, path: Path, category: int) -> ProjectFile:
    """Read one file from a stable native signature or return a deterministic error payload."""

    for _attempt in range(STABLE_READ_ATTEMPTS):
        try:
            _validate_new_project_file(root, path, category)
            before = project_facade._source_signature(path)
        except OSError as error:
            return _read_error_file(root, path, category, error)
        loaded = project_facade.read_project_file(root, path, category)
        try:
            after = project_facade._source_signature(path)
        except OSError as error:
            return _read_error_file(root, path, category, error)
        if before == after:
            return ProjectFile(
                loaded.relative_path,
                loaded.category,
                loaded.payload,
                loaded.content_hash,
                loaded.content_size,
                loaded.source_path or path,
                after,
            )
    return ProjectFile(
        _normalize_relative_path(path.relative_to(root).as_posix()),
        category,
        variant(
            2,
            frontend_error(
                OSError("project file changed repeatedly while it was being read"), IO_CONFLICT
            ),
        ),
        None,
    )


def _verify_stable_files(files: Sequence[ProjectFile]) -> None:
    for item in files:
        if item.source_path is None or item.source_signature is None:
            continue
        try:
            current = project_facade._source_signature(item.source_path)
        except OSError as error:
            raise _ProjectChangedDuringScan(str(error)) from error
        if current != item.source_signature:
            raise _ProjectChangedDuringScan(
                f"project file changed during scan: {item.relative_path}"
            )


def _parallel_ordered(
    items: Sequence[Input],
    operation: Callable[[Input], Output],
    *,
    progress: ProjectScanProgress | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[Output]:
    """Run file work concurrently and publish deterministic coordinator-thread progress."""

    if not items:
        return []
    if cancelled is not None and cancelled():
        raise InterruptedError("project file operation cancelled")
    results: list[Output | BaseException | None] = [None] * len(items)
    pool = ThreadPoolExecutor(
        max_workers=project_facade.PROJECT_IO_WORKERS,
        thread_name_prefix="rustyera-project-io",
    )
    pending: dict[Future[Output], int] = {}
    next_index = 0
    completed = 0
    failed = False
    try:
        while pending or (next_index < len(items) and not failed):
            while (
                len(pending) < project_facade.PROJECT_IO_WORKERS
                and next_index < len(items)
                and not failed
            ):
                if cancelled is not None and cancelled():
                    raise InterruptedError("project file operation cancelled")
                pending[pool.submit(operation, items[next_index])] = next_index
                next_index += 1
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except BaseException as error:  # preserve the first failure in input order
                    results[index] = error
                    failed = True
                completed += 1
                _report_scan_progress(progress, completed, len(items))
            if cancelled is not None and cancelled():
                raise InterruptedError("project file operation cancelled")
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [cast(Output, result) for result in results]


def _parallel_project_reads(
    root: Path,
    candidates: list[tuple[Path, int]],
    progress: ProjectScanProgress | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[ProjectFile]:
    return _parallel_ordered(
        candidates,
        lambda candidate: _stable_read_project_file(root, candidate[0], candidate[1]),
        progress=progress,
        cancelled=cancelled,
    )


def _report_scan_progress(progress: ProjectScanProgress | None, completed: int, total: int) -> None:
    if progress is None:
        return
    if total == 0 or completed == total or completed * 30 // total > (completed - 1) * 30 // total:
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
    name = path.name.lower()
    if name in {"reraconfig.toml", "setting.json"}:
        return FILE_CONFIGURATION
    suffix = path.suffix.lower()
    return {
        ".csv": FILE_CSV,
        ".erh": FILE_ERH,
        ".erb": FILE_ERB,
        ".als": FILE_ALS,
        ".erd": FILE_ERD,
        ".config": FILE_CONFIGURATION,
    }.get(suffix)


def _payload_size(payload: list[Any]) -> int:
    if len(payload) != 2 or not isinstance(payload[1], list) or len(payload[1]) != 1:
        return 0
    value = payload[1][0]
    if isinstance(value, str):
        return utf8_length(value)
    if isinstance(value, bytes):
        return len(value)
    return 0


def read_project_file(root: Path, path: Path, category: int) -> ProjectFile:
    relative = _normalize_relative_path(path.relative_to(root).as_posix())
    try:
        _validate_new_project_file(root, path, category)
        if category == FILE_RESOURCE:
            hasher = blake3.blake3()
            byte_length = 0
            metadata_prefix = bytearray()
            with path.open("rb") as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    hasher.update(chunk)
                    byte_length += len(chunk)
                    if len(metadata_prefix) < 1024 * 1024:
                        needed = 1024 * 1024 - len(metadata_prefix)
                        metadata_prefix.extend(chunk[:needed])
            metadata = None
            if path.suffix.lower() in RESOURCE_IMAGE_SUFFIXES:
                try:
                    from .image_metadata import decode_image_metadata

                    metadata = decode_image_metadata(bytes(metadata_prefix))
                except ValueError:
                    metadata = None
            return ProjectFile(
                relative,
                category,
                project_facade.external_resource(byte_length, metadata),
                hasher.digest(),
                byte_length,
                path,
            )
        raw = path.read_bytes()
        text = _decode_project_source(
            raw,
            strict_utf8=category in (FILE_ALS, FILE_ERD) or relative.lower() == "reraconfig.toml",
        )
        if category == FILE_RESOURCE_MANIFEST:
            text = _normalize_resource_manifest_paths(text)
        hasher = blake3.blake3()
        byte_length = 0
        for chunk in iter_utf8_chunks(text):
            hasher.update(chunk)
            byte_length += len(chunk)
        return ProjectFile(
            relative,
            category,
            variant(0, text),
            hasher.digest(),
            byte_length,
            path,
        )
    except (OSError, UnicodeError) as error:
        return ProjectFile(relative, category, variant(2, frontend_error(error)), None)


def _canonical_source_roots(root: Path) -> frozenset[str]:
    try:
        return frozenset(
            entry.name.lower()
            for entry in root.iterdir()
            if entry.is_dir() and entry.name.lower() in {"csv", "erb"}
        )
    except OSError:
        return frozenset()


def _classify_project_path(root: Path, path: Path, canonical_roots: frozenset[str]) -> int | None:
    parts = path.relative_to(root).parts
    first = parts[0].lower()
    if path.name.lower() in {"reraconfig.toml", "setting.json"}:
        return FILE_CONFIGURATION
    if path.suffix.lower() in RESOURCE_DATA_SUFFIXES:
        return None if first in RESOURCE_DATA_EXCLUDED_ROOTS else FILE_RESOURCE
    if first == "resources":
        if path.suffix.lower() == ".csv":
            return FILE_RESOURCE_MANIFEST
        if path.suffix.lower() in RESOURCE_IMAGE_SUFFIXES | RESOURCE_AUDIO_SUFFIXES:
            return FILE_RESOURCE
        return None
    if first == "sound":
        return FILE_RESOURCE if path.suffix.lower() in RESOURCE_AUDIO_SUFFIXES else None
    if first == "font":
        return FILE_RESOURCE if path.suffix.lower() in RESOURCE_FONT_SUFFIXES else None
    category = classify_path(path)
    if category is None:
        return None
    if category in (FILE_ERH, FILE_ERB, FILE_ERD) and "erb" in canonical_roots and first != "erb":
        return None
    if category == FILE_ALS and canonical_roots and first not in {"csv", "erb"}:
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


def _is_new_project_file(path: Path, category: int) -> bool:
    return category in (FILE_ALS, FILE_ERD) or (
        category == FILE_RESOURCE and path.suffix.lower() in RESOURCE_DATA_SUFFIXES
    )


def _validate_new_project_file(root: Path, path: Path, category: int) -> None:
    """Constrain new input classes without changing legacy source-link support."""

    if not _is_new_project_file(path, category):
        return
    try:
        relative = path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except RuntimeError as error:
        raise OSError(errno.ELOOP, "project input contains a symbolic-link loop") from error
    except ValueError as error:
        raise OSError(errno.EACCES, "project input escaped the authorized root") from error
    if category == FILE_RESOURCE and relative.parts[0].lower() in RESOURCE_DATA_EXCLUDED_ROOTS:
        raise OSError(errno.EACCES, "project resource points into writable or private storage")


def _project_candidates(
    root: Path, paths: Sequence[Path], canonical_roots: frozenset[str]
) -> list[tuple[Path, int]]:
    """Reject normalized collisions before dictionaries can silently discard new inputs."""

    candidates: list[tuple[Path, int]] = []
    seen: dict[str, tuple[Path, int]] = {}
    for path in paths:
        category = _classify_project_path(root, path, canonical_roots)
        if category is None:
            continue
        relative = _normalize_relative_path(path.relative_to(root).as_posix()).lower()
        previous = seen.get(relative)
        if previous is not None and (
            _is_new_project_file(path, category) or _is_new_project_file(*previous)
        ):
            raise ValueError(f"project inputs have duplicate normalized paths: {relative}")
        seen[relative] = (path, category)
        candidates.append((path, category))
    return candidates


def _project_paths(root: Path) -> list[Path]:
    """Enumerate project files while following resource-directory links once."""

    paths: list[Path] = []
    directory_loops: list[Path] = []
    repeated_directories: list[tuple[Path, Path]] = []
    root_stat = root.stat()
    visited = {(root_stat.st_dev, root_stat.st_ino)}

    def report_walk_error(error: OSError) -> None:
        raise error

    for directory, names, filenames in os.walk(root, followlinks=True, onerror=report_walk_error):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in sorted(names, key=str.casefold):
            if name.lower() == ".rustyera":
                continue
            child = directory_path / name
            stat = child.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in visited:
                target = child.resolve(strict=True)
                parent = directory_path.resolve(strict=True)
                if target == parent or target in parent.parents:
                    directory_loops.append(child)
                else:
                    repeated_directories.append((child, target))
                continue
            visited.add(identity)
            retained.append(name)
        names[:] = retained
        paths.extend(directory_path / name for name in filenames)
    canonical_roots = _canonical_source_roots(root)
    for repeated, target in repeated_directories:
        for candidate in target.rglob("*"):
            alias = repeated / candidate.relative_to(target)
            category = _classify_project_path(root, alias, canonical_roots)
            if category is not None and _is_new_project_file(alias, category):
                relative = alias.relative_to(root).as_posix()
                raise ValueError(
                    f"project input is hidden by a repeated directory link: {relative}"
                )
    if directory_loops:
        if any(
            (category := _classify_project_path(root, path, canonical_roots)) is not None
            and _is_new_project_file(path, category)
            for path in paths
        ):
            relative = directory_loops[0].relative_to(root).as_posix()
            raise OSError(errno.ELOOP, f"project input directory contains a loop: {relative}")
    return sorted(
        paths,
        key=lambda path: _path_sort_key(
            _normalize_relative_path(path.relative_to(root).as_posix())
        ),
    )
