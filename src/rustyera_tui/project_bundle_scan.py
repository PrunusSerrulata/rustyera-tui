"""Private ProjectBundle responsibility mixin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import project as project_facade
from .project import (
    Any,
    Callable,
    Path,
    ProjectFile,
    ProjectScanMetrics,
    ProjectScanProgress,
    PurePosixPath,
    STABLE_READ_ATTEMPTS,
    _IndexedCandidate,
    _ProjectChangedDuringScan,
    _canonical_source_roots,
    _classify_project_path,
    _normalize_relative_path,
    _parallel_ordered,
    _parallel_project_reads,
    _project_paths,
    _report_scan_progress,
    _verify_stable_files,
    _write_source_index,
    json,
    time,
)

if TYPE_CHECKING:
    from .project import ProjectBundle


class _ProjectBundleScanMixin:
    @classmethod
    def scan(
        cls,
        root: Path,
        revision: int = 1,
        progress: ProjectScanProgress | None = None,
        cancelled: Callable[[], bool] | None = None,
        _attempt: int = 0,
    ) -> ProjectBundle:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        if progress is not None:
            progress(0, 0)
        if cancelled is not None and cancelled():
            raise InterruptedError("project file operation cancelled")
        started = time.perf_counter()
        paths = _project_paths(root)
        canonical_roots = _canonical_source_roots(root)
        candidates = [
            (path, category)
            for path in paths
            if (category := _classify_project_path(root, path, canonical_roots)) is not None
        ]
        if cancelled is not None and cancelled():
            raise InterruptedError("project file operation cancelled")
        enumerate_ms = (time.perf_counter() - started) * 1000
        if progress is not None:
            progress(0, len(candidates))
        started = time.perf_counter()
        files = {
            item.relative_path: item
            for item in _parallel_project_reads(root, candidates, progress, cancelled)
        }
        try:
            _verify_stable_files(list(files.values()))
        except _ProjectChangedDuringScan:
            if _attempt + 1 >= STABLE_READ_ATTEMPTS:
                raise
            return cls.scan(root, revision, progress, cancelled, _attempt + 1)
        source_ms = (time.perf_counter() - started) * 1000
        return cls(
            root=root,
            revision=revision,
            files=files,
            scan_metrics=ProjectScanMetrics(
                enumerate_ms=enumerate_ms,
                source_read_decode_hash_ms=source_ms,
                source_files_hashed=len(files),
            ),
        )

    @classmethod
    def scan_quick(
        cls,
        root: Path,
        revision: int = 1,
        progress: ProjectScanProgress | None = None,
        cancelled: Callable[[], bool] | None = None,
        _attempt: int = 0,
    ) -> ProjectBundle:
        """Build a content identity using a persistent stat index without retaining source text."""

        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        if progress is not None:
            progress(0, 0)
        if cancelled is not None and cancelled():
            raise InterruptedError("project file operation cancelled")
        metrics = ProjectScanMetrics()
        index_path = root / ".rustyera" / "cache" / "source-index-v1.json"
        index_current = False
        started = time.perf_counter()
        try:
            stored = json.loads(index_path.read_text(encoding="utf-8"))
            index_current = stored.get("version") == 1 and isinstance(stored.get("files"), dict)
            previous = stored["files"] if index_current else {}
        except (OSError, ValueError, TypeError):
            previous = {}
        metrics.index_read_ms = (time.perf_counter() - started) * 1000
        metrics.source_index_present = index_current
        started = time.perf_counter()
        canonical_roots = _canonical_source_roots(root)
        candidates = [
            (path, category)
            for path in _project_paths(root)
            if (category := _classify_project_path(root, path, canonical_roots)) is not None
        ]
        if cancelled is not None and cancelled():
            raise InterruptedError("project file operation cancelled")
        metrics.enumerate_ms = (time.perf_counter() - started) * 1000
        if progress is not None:
            progress(0, len(candidates))

        def inspect(candidate: tuple[Path, int]) -> _IndexedCandidate:
            path, category = candidate
            relative = _normalize_relative_path(path.relative_to(root).as_posix())
            source_signature = project_facade._source_signature(path)
            signature = list(source_signature)
            prior = previous.get(relative)
            if (
                isinstance(prior, dict)
                and prior.get("signature") == signature
                and prior.get("category") == category
                and isinstance(prior.get("size"), int)
            ):
                try:
                    digest = bytes.fromhex(prior["hash"])
                except (TypeError, ValueError):
                    digest = None
                if digest is not None and len(digest) == 32:
                    return _IndexedCandidate(
                        path, category, relative, source_signature, digest, prior["size"]
                    )
            return _IndexedCandidate(path, category, relative, source_signature, None, 0)

        try:
            started = time.perf_counter()
            inspected = _parallel_ordered(candidates, inspect, cancelled=cancelled)
            metrics.stat_ms = (time.perf_counter() - started) * 1000
            invalid = [item for item in inspected if item.content_hash is None]
            started = time.perf_counter()
            loaded = _parallel_ordered(
                invalid,
                lambda item: project_facade._stable_read_project_file(
                    root, item.path, item.category
                ),
                cancelled=cancelled,
            )
            metrics.source_read_decode_hash_ms = (time.perf_counter() - started) * 1000
        except InterruptedError:
            raise
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            # Error payloads and malformed index entries need the normal scanner's precise
            # diagnostic, so do not attempt a cache-only project load.
            return cls.scan(root, revision, progress, cancelled)
        loaded_by_path = {item.relative_path: item for item in loaded}
        if any(item.content_hash is None for item in loaded):
            return cls.scan(root, revision, progress, cancelled)
        indexed: list[tuple[ProjectFile, dict[str, Any]]] = []
        for completed, item in enumerate(inspected, 1):
            project_file = loaded_by_path.get(item.relative_path)
            if project_file is None:
                digest = item.content_hash
                if digest is None:
                    raise RuntimeError(f"project file {item.relative_path} was not read")
                project_file = ProjectFile(
                    item.relative_path,
                    item.category,
                    None,
                    digest,
                    item.content_size,
                    source_path=item.path,
                    source_signature=item.source_signature,
                )
            else:
                project_file = ProjectFile(
                    project_file.relative_path,
                    project_file.category,
                    project_file.payload,
                    project_file.content_hash,
                    project_file.content_size,
                    project_file.source_path,
                    item.source_signature,
                )
            digest = project_file.content_hash
            if digest is None:
                raise ValueError(f"project file {item.relative_path} has no content hash")
            indexed.append(
                (
                    project_file,
                    {
                        "category": item.category,
                        "signature": list(item.source_signature),
                        "hash": digest.hex(),
                        "size": project_file.content_size,
                    },
                )
            )
            _report_scan_progress(progress, completed, len(inspected))
        files = {item.relative_path: item for item, _ in indexed}
        try:
            _verify_stable_files(list(files.values()))
        except _ProjectChangedDuringScan:
            if _attempt + 1 >= STABLE_READ_ATTEMPTS:
                raise
            return cls.scan_quick(root, revision, progress, cancelled, _attempt + 1)
        next_index = {item.relative_path: entry for item, entry in indexed}
        if not index_current or previous != next_index:
            started = time.perf_counter()
            _write_source_index(index_path, {"version": 1, "files": next_index})
            metrics.index_write_ms = (time.perf_counter() - started) * 1000
        metrics.source_files_reused = len(inspected) - len(invalid)
        metrics.source_files_hashed = len(invalid)
        return cls(
            root=root,
            revision=revision,
            files=files,
            quick_scan_pending=True,
            scan_metrics=metrics,
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
        items = list(self.files.values())

        def materialize_one(item: ProjectFile) -> ProjectFile:
            if cancelled is not None and cancelled():
                raise InterruptedError("full project export cancelled")
            materialized = item
            source_path = materialized.source_path or (
                self.root / PurePosixPath(materialized.relative_path)
            )
            try:
                signature_matches = (
                    materialized.source_signature == project_facade._source_signature(source_path)
                )
            except OSError:
                signature_matches = False
            if materialized.payload is None or not signature_matches:
                materialized = project_facade._stable_read_project_file(
                    self.root, source_path, materialized.category
                )
            return materialized

        materialized = _parallel_ordered(
            items,
            materialize_one,
            progress=progress,
            cancelled=cancelled,
        )
        files = {item.relative_path: item for item in materialized}
        return project_facade.ProjectBundle(self.root, self.revision, files, self.project_file)
