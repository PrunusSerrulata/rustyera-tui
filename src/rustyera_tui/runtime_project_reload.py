"""Hot-reload responsibilities for the RuntimeClient project mixin."""

from __future__ import annotations

from .runtime_dependencies import Any, FrontendEvent, Path, ProjectBundle


class _RuntimeProjectReloadMixin:
    def reload_all(self) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        candidate, request = self.bundle.rescan(self._project_scan_progress)
        self._submit_reload(candidate, request, f"{len(request[2])} 个文件变更")

    def reload_folder(self, path: Path) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        candidate, request = self.bundle.reload_folder(path, self._project_scan_progress)
        self._submit_reload(candidate, request, path.name or str(path))

    def reload_file(self, path: Path) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        candidate, request = self.bundle.reload_file(path)
        self._submit_reload(candidate, request, path.name)

    def _submit_reload(
        self, candidate: ProjectBundle, request: dict[int, Any], description: str
    ) -> None:
        if not request[2]:
            if self.bundle is None:
                raise RuntimeError("no project is active")
            candidate.revision = self.bundle.revision
            self.bundle = candidate
            self.events.put(FrontendEvent("status", "脚本热重载完成；没有文件发生变化。"))
            return
        self.reload_candidate = candidate
        self.events.put(FrontendEvent("status", f"正在热重载 {description}…"))
        self.reload_message_id = self.send_runtime(12, request)
