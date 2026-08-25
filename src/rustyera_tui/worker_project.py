"""Project-loading responsibilities for the threaded Runtime worker."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from .project import ProjectBundle
from .runtime_types import FrontendEvent

if TYPE_CHECKING:
    from .worker import RuntimeWorker


class _WorkerProjectMixin:
    def _load_project(self: RuntimeWorker, root: Path) -> None:
        if self.client is None:
            return
        self.client.begin_startup_attempt(project_file=False)
        self.client.begin_session_reset()
        self.events.put(FrontendEvent("status", f"正在扫描 {root}…"))
        bundle = ProjectBundle.scan_quick(
            root,
            1,
            self._emit_scan_progress,
            cancelled=self._stop_requested.is_set,
        )
        if self._stop_requested.is_set():
            return
        self.client.source_index_misses = bundle.scan_metrics.source_index_misses
        self.client.record_host_metrics(bundle.scan_metrics.telemetry())
        restore = None
        if self.initial_state is not None:
            path, purpose = self.initial_state
            resolved = path.expanduser().resolve(strict=True)
            restore = (resolved, resolved.read_bytes(), purpose)
            self.initial_state = None
        self.client.recreate(bundle, restore)

    def _load_project_file(self: RuntimeWorker, path: Path) -> None:
        if self.client is None:
            return
        self.client.begin_startup_attempt(project_file=True)
        self.client.begin_session_reset()
        started = time.monotonic_ns()
        resolved = path.expanduser().resolve(strict=True)
        payload = resolved.read_bytes()
        self.client.prepare_replacement_session()
        try:
            manifest = self.client.abi.project_file_manifest(payload)
            self.client.record_host_duration("cache_read_ms", started)
            bundle = ProjectBundle.from_project_file_manifest(resolved, manifest)
            self.events.put(FrontendEvent("status", f"正在载入项目文件 {resolved}…"))
            self.client.recreate(bundle, project_file_bytes=payload)
        except BaseException as error:
            if self.client._replacement_session_prepared:
                self.client.abort_session_replacement(error)
            raise

    def _emit_scan_progress(self: RuntimeWorker, completed: int, total: int) -> None:
        self.events.put(FrontendEvent("project_progress", (0, completed, total)))

    def _emit_project_progress(self: RuntimeWorker, stage: int, completed: int, total: int) -> None:
        if self.client is not None:
            self.client.report_runtime_project_progress(stage, completed, total)
        else:
            self.events.put(FrontendEvent("project_progress", (stage, completed, total)))
