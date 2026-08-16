"""Private RustyEraTui responsibilities extracted from the app facade."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual.widgets import Button, ProgressBar, Static

from .dialogs import (
    AboutDialog,
    ConfirmDialog,
    ExportProgressDialog,
    PathDialog,
    PreferencesDialog,
    ProjectSettingsDialog,
)
from .app_export_paths import log_default_path, project_file_default_path, snapshot_default_path
from .diagnosis import diagnosis_default_path
from .log_model import format_log_entries
from .presentation import DEFAULT_PRESENTATION_TITLE
from .runtime import DiagnosisProgress
from .widgets import GameViewport


class _MenuAndExportMixin:
    def _set_debug_enabled(self, enabled: bool) -> None:
        self.debug_enabled = enabled
        self.query_one("#debug-toggle", Button).label = "禁用调试" if enabled else "启用调试"
        self._refresh_menu_availability()
        if not enabled:
            self.single_step = False
            self.debug_paused = False
            self.debug_location = None
            self.query_one("#debug-step-toggle", Button).label = "开启单步运行"
            self._update_prompt()
            self._refresh_interaction_lock()

    def _runtime_menu_actions_available(self) -> bool:
        return self.runtime_phase in self.GAME_READY_PHASES

    def _refresh_menu_availability(self) -> None:
        if not self.is_mounted:
            return
        screen = self.screen_stack[0]
        available = self._runtime_menu_actions_available()
        for item_id in self.GAME_FILE_ITEMS:
            screen.query_one(f"#{item_id}", Button).disabled = not available
        screen.query_one("#file-project-settings", Button).disabled = (
            not available or self.configuration_snapshot is None
        )
        screen.query_one("#debug-toggle", Button).disabled = not available
        for item_id in self.GAME_DEBUG_ITEMS[1:]:
            screen.query_one(f"#{item_id}", Button).disabled = (
                not available or not self.debug_enabled
            )
        screen.query_one("#help-export-diagnosis", Button).disabled = not available

    def on_button_pressed(self, event: Button.Pressed) -> None:
        item_id = event.button.id or ""
        if item_id == "menu-file":
            self._toggle_menu("file")
            return
        if item_id == "menu-debug":
            self._toggle_menu("debug")
            return
        if item_id == "menu-help":
            self._toggle_menu("help")
            return
        if item_id.startswith("file-"):
            self.action_close_menus()
            self._file_action(item_id)
        elif item_id.startswith("debug-"):
            self.action_close_menus()
            self._debug_action(item_id)
        elif item_id.startswith("help-"):
            self.action_close_menus()
            self._help_action(item_id)

    def _toggle_menu(self, name: str) -> None:
        selected = self.query_one(f"#{name}-menu")
        for candidate in ("file", "debug", "help"):
            if candidate != name:
                self.query_one(f"#{candidate}-menu").remove_class("visible")
        selected.toggle_class("visible")

    def action_close_menus(self) -> None:
        self.query_one("#file-menu").remove_class("visible")
        self.query_one("#debug-menu").remove_class("visible")
        self.query_one("#help-menu").remove_class("visible")

    def _file_action(self, item_id: str) -> None:
        if (self.snapshot_exporting or self.project_file_exporting) and item_id != "file-exit":
            self.notify("文件导出完成前不能执行此操作", severity="warning")
            return
        if item_id == "file-restart":
            self._confirm_progress_loss("restart", "重新开始游戏", "重新开始")
        elif item_id == "file-title":
            self._confirm_progress_loss("return_title", "返回标题", "返回标题")
        elif item_id == "file-preferences":
            self.action_preferences()
        elif item_id == "file-project-settings":
            self.action_project_settings()
        elif item_id == "file-reload-all":
            self.worker.send("reload_all")
        elif item_id == "file-reload-folder":
            self._choose_path("重新加载文件夹", "directory", "reload_folder")
        elif item_id == "file-reload-file":
            self._choose_path("重新加载单个脚本", "file", "reload_file")
        elif item_id == "file-export-project":
            self.push_screen(
                PathDialog("导出全量项目文件", "save", self._project_file_default_path()),
                self._start_project_file_export,
            )
        elif item_id == "file-export-snapshot":
            initial = self._snapshot_default_path()
            self.push_screen(
                PathDialog("导出当前 VM 快照", "save", initial),
                self._start_snapshot_export,
            )
        elif item_id == "file-restore-snapshot":
            self._choose_path("恢复 VM 快照", "file", "restore_snapshot")
        elif item_id == "file-exit":
            self.action_request_quit()

    def _confirm_progress_loss(
        self, command: str, action_description: str, confirm_label: str
    ) -> None:
        dialog = ConfirmDialog(
            action_description,
            f"{action_description}可能会丢失尚未保存的游戏进度。\n确定要继续吗？",
            confirm_label,
        )
        expected_phase = self.runtime_phase
        self.progress_loss_dialog = dialog
        self.push_screen(
            dialog,
            lambda confirmed: self._complete_progress_loss_confirmation(
                dialog, expected_phase, command, confirmed
            ),
        )

    def _complete_progress_loss_confirmation(
        self,
        dialog: ConfirmDialog,
        expected_phase: int,
        command: str,
        confirmed: bool,
    ) -> None:
        if self.progress_loss_dialog is dialog:
            self.progress_loss_dialog = None
        if self.is_mounted:
            self.query_one("#menu-file", Button).focus()
        if (
            confirmed
            and self.runtime_phase == expected_phase
            and self._runtime_menu_actions_available()
            and self.blocking_error is None
            and not self.project_progress_blocks_interaction
        ):
            self.worker.send(command)

    def _cancel_progress_loss_confirmation(self) -> None:
        dialog = self.progress_loss_dialog
        if dialog is None:
            return
        self.progress_loss_dialog = None
        if dialog.is_mounted:
            dialog.dismiss(False)
        if self.is_mounted:
            self.query_one("#menu-file", Button).focus()

    def action_preferences(self) -> None:
        self.push_screen(
            PreferencesDialog(
                self.configuration_snapshot,
                self.global_preferences,
                self.project_preferences,
            )
        )

    def action_project_settings(self) -> None:
        if self.configuration_snapshot is None:
            self.notify("Runtime 尚未提供项目配置", severity="warning")
            return
        self.push_screen(
            ProjectSettingsDialog(self.configuration_snapshot, self.configuration_read_only)
        )

    def on_project_settings_dialog_apply_requested(
        self, event: ProjectSettingsDialog.ApplyRequested
    ) -> None:
        if event.restart and not event.changes:
            if isinstance(self.screen, ProjectSettingsDialog):
                self.screen.dismiss()
            self.worker.send("restart")
            return
        self.worker.send("save_configuration", (event.changes, event.restart))

    def on_preferences_dialog_save_requested(self, event: PreferencesDialog.SaveRequested) -> None:
        self.worker.send("save_client_preferences", (event.scope, event.values))

    def _apply_client_preferences(self) -> None:
        if self.configuration_snapshot is None or not self.is_mounted:
            return
        snapshot = self.configuration_snapshot
        screen = self.screen_stack[0]
        screen.query_one("#menu-bar").display = True
        viewport = screen.query_one(GameViewport)
        viewport.set_mouse_enabled(snapshot.client_effective_value("UseMouse", "YES") == "YES")
        viewport.set_replace_full_width_spaces(
            snapshot.client_effective_value("ReplaceFullWidthSpaces", "NO") == "YES"
        )

    def _reset_client_preferences(self) -> None:
        if not self.is_mounted:
            return
        screen = self.screen_stack[0]
        screen.query_one("#menu-bar").display = True
        viewport = screen.query_one(GameViewport)
        viewport.set_mouse_enabled(True)
        viewport.set_replace_full_width_spaces(False)

    def _choose_path(self, title: str, mode: str, command: str) -> None:
        initial = self.project or Path.cwd()
        self.push_screen(
            PathDialog(title, mode, initial),
            lambda path: path and self.worker.send(command, path),
        )

    def _snapshot_default_path(self, now: datetime | None = None) -> Path:
        return snapshot_default_path(self.project, now)

    def _project_file_default_path(self) -> Path:
        presentation_title = (
            "" if self.presentation.title == DEFAULT_PRESENTATION_TITLE else self.presentation.title
        )
        return project_file_default_path(self.project, presentation_title)

    def _log_default_path(self, now: datetime | None = None) -> Path:
        return log_default_path(self.project, now)

    def _export_logs(self, path: Path | None, contents: str) -> None:
        if path is None:
            return
        try:
            path.write_text(contents, encoding="utf-8")
        except OSError as error:
            self.notify(f"日志导出失败：{error}", severity="error")
        else:
            self.notify(f"日志已导出：{path}")

    def _start_snapshot_export(self, path: Path | None) -> None:
        if path is None or self.snapshot_exporting:
            return
        self.snapshot_exporting = True
        self._update_prompt()
        self._refresh_interaction_lock()
        purpose = "debug" if self.debug_enabled else "normal"
        self.worker.send("export_snapshot", (path, purpose))

    def _start_project_file_export(self, path: Path | None) -> None:
        if path is None or self.project_file_exporting:
            return
        self.project_file_exporting = True
        self._update_prompt()
        self._refresh_interaction_lock()
        self.export_progress_dialog = ExportProgressDialog()
        self.push_screen(self.export_progress_dialog, self._finish_project_file_progress_dialog)
        self.worker.send("export_project_file", path)

    def _finish_project_file_progress_dialog(self, completed: bool | None) -> None:
        if completed or not self.project_file_exporting:
            return
        self.export_progress_dialog = None
        self.project_file_exporting = False
        self._update_prompt()
        self._refresh_interaction_lock()
        self.worker.send("cancel_project_file_export")

    def _start_diagnosis_export(self, path: Path | None) -> None:
        if path is None or self.diagnosis_exporting:
            return
        self.diagnosis_exporting = True
        self.diagnosis_export_at_fault = bool(
            self.fatal_dialog is not None and self.fatal_dialog.is_mounted
        )
        self._update_prompt()
        self._refresh_interaction_lock()
        if self.diagnosis_export_at_fault and self.fatal_dialog is not None:
            self.fatal_dialog.set_exporting()
        else:
            self._show_diagnosis_progress()
        self.fault_logs = format_log_entries(self.logs)
        self.worker.send(
            "export_diagnosis",
            (path, self.fault_logs, self._diagnosis_project_title()),
        )

    def _update_diagnosis_progress(self, progress: DiagnosisProgress) -> None:
        if not self.diagnosis_exporting:
            return
        label = self.DIAGNOSIS_PROGRESS_LABELS.get(progress.stage, "正在导出诊断信息")
        if progress.total > 0:
            completed = min(progress.completed, progress.total)
            percent = min(100, completed * 100 // progress.total)
            message = f"{label}（{percent}%）"
        else:
            completed = 0
            message = f"{label}…"
        if self.diagnosis_export_at_fault and self.fatal_dialog is not None:
            if self.fatal_dialog.is_mounted:
                self.fatal_dialog.update_export_progress(
                    message,
                    completed,
                    progress.total,
                )
                return
            self.diagnosis_export_at_fault = False
        self._show_diagnosis_progress()
        self.query_one("#diagnosis-progress-label", Static).update(message)
        self.query_one("#diagnosis-progress-bar", ProgressBar).update(
            progress=completed,
            total=progress.total if progress.total > 0 else None,
        )

    def _show_diagnosis_progress(self) -> None:
        if self.is_mounted:
            self.query_one("#diagnosis-progress").add_class("visible")

    def _hide_diagnosis_progress(self) -> None:
        if self.is_mounted:
            self.query_one("#diagnosis-progress").remove_class("visible")

    def _open_diagnosis_export_dialog(self) -> None:
        initial = diagnosis_default_path(
            self.project or Path.cwd(),
            project_name=self._diagnosis_project_title(),
        )
        self.push_screen(
            PathDialog("导出诊断信息", "save", initial),
            self._start_diagnosis_export,
        )

    def _diagnosis_project_title(self) -> str:
        presentation_title = (
            "" if self.presentation.title == DEFAULT_PRESENTATION_TITLE else self.presentation.title
        )
        project_path_name = self.project_file.stem if self.project_file else self.project.name
        return next(
            (
                candidate.strip()
                for candidate in (
                    self.game_information.title,
                    presentation_title,
                    project_path_name,
                    "project",
                )
                if candidate and candidate.strip()
            )
        )

    def _help_action(self, item_id: str) -> None:
        from .app import frontend_version

        if item_id == "help-export-diagnosis":
            self._open_diagnosis_export_dialog()
        elif item_id == "help-about":
            self.push_screen(
                AboutDialog(frontend_version(), self.core_version, self.game_information)
            )
