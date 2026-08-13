"""Project scanning, stable reads, path classification, and source index helpers."""

from __future__ import annotations

from . import project as project_facade
from .project import (
    Any,
    Callable,
    FILE_CONFIGURATION,
    FILE_CSV,
    FILE_ERB,
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
    RESOURCE_FONT_SUFFIXES,
    RESOURCE_IMAGE_SUFFIXES,
    STABLE_READ_ATTEMPTS,
    Sequence,
    ThreadPoolExecutor,
    _ProjectChangedDuringScan,
    blake3,
    cast,
    frontend_error,
    json,
    os,
    tempfile,
    unicodedata,
    variant,
    wait,
)


def _read_error_file(
    root: Path, path: Path, category: int, error: OSError | UnicodeError
) -> ProjectFile:
    relative = _normalize_relative_path(path.relative_to(root).as_posix())
    return ProjectFile(relative, category, variant(2, frontend_error(error)), None)


def _stable_read_project_file(root: Path, path: Path, category: int) -> ProjectFile:
    """Read one file from a stable native signature or return a deterministic error payload."""

    for _attempt in range(STABLE_READ_ATTEMPTS):
        try:
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
        ".config": FILE_CONFIGURATION,
    }.get(suffix)


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
        text = _decode_project_source(raw, strict_utf8=relative.lower() == "reraconfig.toml")
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


def _path_sort_key(path: str) -> tuple[str, str]:
    """Match the runtime's locale-independent lowercase/path ordering."""

    return path.lower(), path


def _normalize_resource_manifest_paths(text: str) -> str:
    normalized: list[str] = []
    for body, ending in _resource_manifest_lines(text):
        fields = body.split(",")
        if len(fields) >= 2:
            value = fields[1]
            stripped = value.strip(" \t")
            if stripped and stripped.lower() != "anime":
                leading = value[: len(value) - len(value.lstrip(" \t"))]
                trailing = value[len(value.rstrip(" \t")) :]
                fields[1] = f"{leading}{unicodedata.normalize('NFC', stripped)}{trailing}"
        normalized.append(",".join(fields) + ending)
    return "".join(normalized)


def _resource_manifest_lines(text: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    start = 0
    while start < len(text):
        cr = text.find("\r", start)
        lf = text.find("\n", start)
        endings = [offset for offset in (cr, lf) if offset >= 0]
        if not endings:
            lines.append((text[start:], ""))
            break
        ending_start = min(endings)
        ending_end = ending_start + (2 if text.startswith("\r\n", ending_start) else 1)
        lines.append((text[start:ending_start], text[ending_start:ending_end]))
        start = ending_end
    return lines


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
            if name.lower() == ".rustyera":
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
        key=lambda path: _path_sort_key(
            _normalize_relative_path(path.relative_to(root).as_posix())
        ),
    )
