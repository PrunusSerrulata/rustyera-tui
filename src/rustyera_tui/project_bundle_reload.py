"""Private ProjectBundle responsibility mixin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import project as project_facade
from .project import (
    Any,
    FILE_CONFIGURATION,
    FILE_CSV,
    FILE_ERB,
    FILE_ERH,
    Path,
    ProjectConfigurationUpdate,
    ProjectFile,
    ProjectScanProgress,
    PurePosixPath,
    _atomic_write_text,
    _canonical_source_roots,
    _classify_project_path,
    _decode_project_source,
    _normalize_relative_path,
    _parallel_ordered,
    _parallel_project_reads,
    _path_sort_key,
    _project_paths,
    blake3,
    os,
    variant,
)

if TYPE_CHECKING:
    from .project import ProjectBundle


class _ProjectBundleReloadMixin:
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
                current_hasher = blake3.blake3()
                while chunk := stream.read(4 * 1024 * 1024):
                    current_hasher.update(chunk)
                if current_hasher.digest() != project_digest:
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
                and item.relative_path.lower() == "reraconfig.toml"
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

    def rescan(
        self, progress: ProjectScanProgress | None = None
    ) -> tuple[ProjectBundle, dict[int, Any]]:
        if self.project_file is not None:
            raise RuntimeError("a packaged project cannot reload source files")
        candidate = project_facade.ProjectBundle.scan(self.root, self.revision + 1, progress)
        changes: list[Any] = []
        for relative_path in sorted(set(self.files) | set(candidate.files), key=_path_sort_key):
            old = self.files.get(relative_path)
            new = candidate.files.get(relative_path)
            if new is None and old is not None:
                changes.append(variant(1, old.category, relative_path))
            elif new is not None and (
                old is None or new.category != old.category or new.content_hash != old.content_hash
            ):
                changes.append(variant(0, new.submitted()))
        reload_request = {0: self.revision, 1: candidate.revision, 2: changes}
        project_facade.ProjectBundle.scan_quick(self.root, candidate.revision)
        return self._hydrate_reload_baseline(candidate, reload_request)

    def reload_folder(
        self,
        path: Path,
        progress: ProjectScanProgress | None = None,
    ) -> tuple[ProjectBundle, dict[int, Any]]:
        if self.project_file is not None:
            raise RuntimeError("a packaged project cannot reload source files")
        folder = self._project_relative_path(path, expected="folder")
        prefix = "" if folder == "." else f"{folder}/"
        scanned_files = self._scan_folder_sync(folder, progress)
        selected = set(scanned_files) | {
            relative for relative in self.files if not prefix or relative.startswith(prefix)
        }
        candidate, request = self._reload_selected(scanned_files, selected)
        project_facade.ProjectBundle.scan_quick(self.root, candidate.revision)
        return self._hydrate_reload_baseline(candidate, request)

    def _scan_folder_sync(
        self,
        folder: str,
        progress: ProjectScanProgress | None,
    ) -> dict[str, ProjectFile]:
        directory = self.root if folder == "." else self.root / PurePosixPath(folder)
        canonical_roots = frozenset(
            PurePosixPath(relative).parts[0].lower()
            for relative in self.files
            if PurePosixPath(relative).parts[0].lower() in {"csv", "erb"}
        )
        candidates = [
            (path, category)
            for path in _project_paths(directory)
            if (category := _classify_project_path(self.root, path, canonical_roots)) is not None
        ]
        if progress is not None:
            progress(0, len(candidates))
        return {
            item.relative_path: item
            for item in _parallel_project_reads(self.root, candidates, progress)
        }

    def _reload_selected(
        self,
        scanned: dict[str, ProjectFile],
        selected: set[str],
    ) -> tuple[ProjectBundle, dict[int, Any]]:
        files = dict(self.files)
        changes: list[Any] = []
        for relative in sorted(selected, key=_path_sort_key):
            old = self.files.get(relative)
            new = scanned.get(relative)
            if new is None and old is not None:
                files.pop(relative, None)
                changes.append(variant(1, old.category, relative))
            elif new is not None and (
                old is None or new.category != old.category or new.content_hash != old.content_hash
            ):
                files[relative] = new
                changes.append(variant(0, new.submitted()))
        candidate = project_facade.ProjectBundle(
            self.root,
            self.revision + 1,
            files,
            quick_scan_pending=any(item.payload is None for item in files.values()),
        )
        return candidate, {0: self.revision, 1: candidate.revision, 2: changes}

    def reload_file(self, path: Path) -> tuple[ProjectBundle, dict[int, Any]]:
        if self.project_file is not None:
            raise RuntimeError("a packaged project cannot reload source files")
        relative = self._project_relative_path(path, expected="file")
        lexical = self.root / PurePosixPath(relative)
        category = _classify_project_path(self.root, lexical, _canonical_source_roots(self.root))
        if category not in (FILE_CSV, FILE_ERH, FILE_ERB, FILE_CONFIGURATION):
            raise ValueError("only project source and configuration files can be reloaded")
        item = project_facade._stable_read_project_file(self.root, lexical, category)
        files = dict(self.files)
        files[relative] = item
        candidate = project_facade.ProjectBundle(
            self.root,
            self.revision + 1,
            files,
            quick_scan_pending=any(value.payload is None for value in files.values()),
        )
        request = {
            0: self.revision,
            1: candidate.revision,
            2: [variant(0, item.submitted())],
        }
        project_facade.ProjectBundle.scan_quick(self.root, candidate.revision)
        return self._hydrate_reload_baseline(candidate, request)

    def _hydrate_reload_baseline(
        self,
        candidate: ProjectBundle,
        request: dict[int, Any],
    ) -> tuple[ProjectBundle, dict[int, Any]]:
        if not self.reload_baseline_pending:
            return candidate, request
        sparse = [item for item in candidate.files.values() if item.payload is None]

        def hydrate_if_unchanged(item: ProjectFile) -> ProjectFile:
            source_path = item.source_path or self.root / PurePosixPath(item.relative_path)
            loaded = project_facade._stable_read_project_file(self.root, source_path, item.category)
            return loaded if loaded.content_hash == item.content_hash else item

        hydrated = _parallel_ordered(sparse, hydrate_if_unchanged)
        files = dict(candidate.files)
        files.update((item.relative_path, item) for item in hydrated)
        candidate.files = files
        candidate.quick_scan_pending = any(item.payload is None for item in files.values())
        candidate.reload_baseline_pending = candidate.quick_scan_pending
        if not candidate.quick_scan_pending:
            removals = [
                variant(1, self.files[relative_path].category, relative_path)
                for relative_path in sorted(set(self.files).difference(files), key=_path_sort_key)
            ]
            request[2] = [
                variant(0, item.submitted())
                for item in sorted(
                    files.values(), key=lambda item: _path_sort_key(item.relative_path)
                )
            ] + removals
        return candidate, request

    def _project_relative_path(self, path: Path, *, expected: str) -> str:
        expanded = path.expanduser()
        absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
        lexical = Path(os.path.abspath(absolute))
        if expected == "file" and not lexical.is_file():
            raise FileNotFoundError(lexical)
        if expected == "folder" and not lexical.is_dir():
            raise NotADirectoryError(lexical)
        try:
            return _normalize_relative_path(lexical.relative_to(self.root).as_posix())
        except ValueError as error:
            raise ValueError(f"the {expected} must be inside the active project") from error
