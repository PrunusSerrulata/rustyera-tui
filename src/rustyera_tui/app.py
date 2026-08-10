"""Textual application shell for the RustyEra runtime frontend."""

from __future__ import annotations

import queue
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Rule, Static

from .configuration import ConfigurationSnapshot
from .dialogs import (
    AboutDialog,
    ConfirmDialog,
    DebugConsoleDialog,
    ExportProgressDialog,
    FatalErrorDialog,
    LogDialog,
    PathDialog,
    PreferencesDialog,
    StackDialog,
    VariableDialog,
    VariableRefresh,
)
from .diagnosis import diagnosis_default_path, diagnosis_project_name
from .input_policy import is_message_skip_wait, is_message_wait
from .log_model import LogEntry, LogLevel, LogMessage, format_log_entries, make_log_entry
from .presentation import PresentationModel
from .runtime import FrontendEvent, PresentationBatch, RuntimeWorker
from .widgets import GameLine, GameViewport

CORE_VERSION = "0.3.0 (1db0a4ce)"


def frontend_version() -> str:
    try:
        return version("rustyera-tui")
    except PackageNotFoundError:
        return "0.3.0"


class RustyEraTui(App[None]):
    """Responsive TUI that never shares internal runtime objects with the frontend."""

    CSS_PATH = "app.tcss"
    TITLE = "RustyEra TUI"
    ENABLE_COMMAND_PALETTE = False
    PROMPT_STATE_CLASSES = (
        "prompt-running",
        "prompt-running-bright",
        "prompt-number",
        "prompt-enter",
        "prompt-other",
        "prompt-error",
    )
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "退出", priority=True),
        Binding("ctrl+z", "input_undo", "撤销输入"),
        Binding("ctrl+comma", "preferences", "偏好选项"),
        Binding("f10", "debug_step", "单步"),
        Binding("escape", "close_menus", "关闭菜单", show=False),
    ]

    FILE_ITEMS = (
        ("file-restart", "重启"),
        ("file-title", "返回标题画面"),
        ("file-preferences", "偏好选项..."),
        ("file-reload-all", "重新载入所有脚本"),
        ("file-reload-folder", "重新载入文件夹..."),
        ("file-reload-file", "重新载入脚本文件..."),
        ("file-export-project", "导出全量项目文件..."),
        ("file-export-snapshot", "导出当前VM快照..."),
        ("file-restore-snapshot", "恢复VM快照..."),
        ("file-exit", "退出"),
    )
    GAME_READY_PHASES = {4, 5, 6, 7, 10, 11}
    GAME_FILE_ITEMS = (
        "file-restart",
        "file-title",
        "file-reload-all",
        "file-reload-folder",
        "file-reload-file",
        "file-export-project",
        "file-export-snapshot",
        "file-restore-snapshot",
    )
    GAME_DEBUG_ITEMS = (
        "debug-toggle",
        "debug-console",
        "debug-variables",
        "debug-stack",
        "debug-step-toggle",
    )
    PROJECT_PROGRESS_PREFIXES = (
        "正在创建新的 Runtime session",
        "正在扫描 ",
        "正在载入项目缓存",
        "项目缓存未命中",
        "正在提交项目并编译脚本",
        "项目编译完成，正在进入标题画面",
        "项目缓存命中，正在进入标题画面",
        "项目缓存已保存，正在进入标题画面",
        "项目缓存保存失败，正在进入标题画面",
        "正在热重载",
    )
    PROJECT_PROGRESS_LABELS = (
        "正在读取项目文件",
        "正在整理项目文件",
        "正在加载项目数据",
        "正在解析脚本",
        "正在分析脚本",
        "正在编译脚本函数",
        "正在验证编译结果",
        "正在整理编译结果",
        "正在准备 Runtime 资源",
        "正在打包全量项目文件",
    )

    def __init__(
        self,
        resource_directory: Path | None,
        runtime_library: Path | None,
        project_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.project = (
            resource_directory.expanduser()
            if resource_directory
            else project_file.expanduser().parent
            if project_file
            else Path.cwd()
        )
        self.project_name = ""
        self.runtime_library = runtime_library
        self.worker = RuntimeWorker(
            runtime_library,
            resource_directory,
            initial_project_file=project_file,
        )
        self.presentation = PresentationModel()
        self.active_wait: dict[int, Any] | None = None
        self._activated_wait: tuple[int, Any] | None = None
        self._pending_retired_buttons: set[tuple[int, int]] = set()
        self.blocking_error: str | None = None
        self.input_undo_token: dict[int, Any] | None = None
        self.logs: list[LogEntry] = []
        self.debug_enabled = False
        self.single_step = False
        self.debug_paused = False
        self.debug_location: str | None = None
        self.runtime_phase = 0
        self.environment_revision = 0
        self.exit_pending = False
        self.snapshot_exporting = False
        self.project_file_exporting = False
        self.export_progress_dialog: ExportProgressDialog | None = None
        self.diagnosis_exporting = False
        self.presentation_rendering = False
        self._presentation_dirty = False
        self._presentation_commit_ready = False
        self._projection_refresh_scheduled = False
        self.variable_dialog: VariableDialog | None = None
        self.stack_dialog: StackDialog | None = None
        self.console_dialog: DebugConsoleDialog | None = None
        self.fatal_dialog: FatalErrorDialog | None = None
        self.progress_loss_dialog: ConfirmDialog | None = None
        self.fault_logs = ""
        self.project_progress_active = False
        self.project_progress_blocks_interaction = False
        self.project_progress_message = ""
        self.configuration_snapshot: ConfigurationSnapshot | None = None
        self.configuration_read_only = False

    def compose(self) -> ComposeResult:
        with Vertical(id="app-root"):
            with Horizontal(id="menu-bar"):
                yield Button("文件", id="menu-file", classes="menu-button")
                yield Button("调试", id="menu-debug", classes="menu-button")
                yield Button("帮助", id="menu-help", classes="menu-button")
            yield GameViewport()
            yield Rule(id="separator-line")
            with Horizontal(id="prompt-row"):
                yield Static("> ", id="prompt-label", classes="prompt-running")
                yield Input(placeholder="等待 Runtime…", id="prompt", disabled=True)
        with Vertical(id="file-menu", classes="dropdown"):
            for item_id, label in self.FILE_ITEMS:
                yield Button(label, id=item_id, classes="menu-item")
        with Vertical(id="debug-menu", classes="dropdown"):
            yield Button("开启调试模式", id="debug-toggle", classes="menu-item")
            yield Button("调试控制台...", id="debug-console", classes="menu-item", disabled=True)
            yield Button("变量查看...", id="debug-variables", classes="menu-item", disabled=True)
            yield Button("栈查看...", id="debug-stack", classes="menu-item", disabled=True)
            yield Button("开启单步运行", id="debug-step-toggle", classes="menu-item", disabled=True)
            yield Button("查看日志...", id="debug-logs", classes="menu-item")
        with Vertical(id="help-menu", classes="dropdown"):
            yield Button("导出诊断信息…", id="help-export-diagnosis", classes="menu-item")
            yield Rule(classes="menu-separator")
            yield Button("关于…", id="help-about", classes="menu-item")

    def on_mount(self) -> None:
        self.worker.start()
        self.set_interval(0.03, self._drain_worker_events)
        self.set_interval(0.5, self._toggle_prompt_blink)
        self._update_prompt()
        self._refresh_menu_availability()
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        if self.worker.is_alive():
            self.worker.stop()
            self.worker.join(timeout=2)

    async def _drain_worker_events(self) -> None:
        queue_exhausted = False
        for _ in range(1000):
            try:
                event = self.worker.events.get_nowait()
            except queue.Empty:
                queue_exhausted = True
                break
            dirty = self._handle_worker_event(event)
            if dirty and not self._presentation_dirty:
                self._begin_presentation_render()
            self._presentation_dirty = self._presentation_dirty or dirty
        if queue_exhausted and self._presentation_dirty and self._presentation_commit_ready:
            await self._commit_presentation()

    async def _commit_presentation(self) -> None:
        try:
            viewport = self.query_one(GameViewport)
        except NoMatches:
            self._presentation_dirty = False
            self._presentation_commit_ready = False
            self.presentation_rendering = False
            return
        changed_from, trimmed_prefix = self.presentation.take_render_change()
        with self.batch_update():
            viewport.set_button_focus(self.presentation.button_focus)
            horizontal_overflow = await viewport.set_lines(
                self.presentation.lines,
                changed_from=changed_from,
                trimmed_prefix=trimmed_prefix,
            )
            self.query_one("#separator-line").display = not horizontal_overflow
            self.title = self.presentation.title or self.TITLE
            viewport.set_presentation_background(self.presentation.background)
        revision = self.presentation.revision
        self._presentation_dirty = False
        self._presentation_commit_ready = False
        self._schedule_viewport_projection()
        self.call_after_refresh(self._finish_presentation_render, revision)

    def _handle_worker_event(self, event: FrontendEvent) -> bool:
        kind, value = event.kind, event.value
        if kind == "presentation_batch":
            if not isinstance(value, PresentationBatch):
                self._log("worker returned an invalid presentation batch", LogLevel.WARNING)
                return False
            dirty = False
            if value.snapshot is not None:
                self.presentation.apply_snapshot(value.snapshot)
                self._capture_project_name()
                dirty = True
            if value.delta is not None:
                try:
                    self.presentation.apply_delta(value.delta)
                except ValueError as error:
                    self._log(str(error), LogLevel.WARNING)
                else:
                    self._capture_project_name()
                    dirty = True
            self._set_active_wait(value.active_wait)
            self._presentation_commit_ready = value.render
            return dirty
        if kind == "wait":
            self._set_active_wait(value)
        elif kind == "input_undo":
            self.input_undo_token = value.get(4) if value.get(0) else None
        elif kind == "text_box":
            prompt = self.query_one("#prompt", Input)
            if not prompt.value:
                prompt.value = value
        elif kind == "phase":
            if int(value) != self.runtime_phase:
                self._cancel_progress_loss_confirmation()
            self.runtime_phase = int(value)
            if self.runtime_phase != 7:
                self.debug_paused = False
                self.debug_location = None
            if value != 11 and self.blocking_error is not None:
                self.blocking_error = None
            self._update_prompt()
            self._refresh_menu_availability()
            self._refresh_interaction_lock()
            if self.project_progress_active and self.runtime_phase in {
                4,
                5,
                10,
                11,
            }:
                self._finish_project_progress()
        elif kind == "status":
            self._set_status(str(value))
        elif kind == "project_progress":
            self._update_project_progress(*value)
        elif kind == "project_progress_finished":
            self._finish_project_progress()
        elif kind == "snapshot_export_finished":
            self.snapshot_exporting = False
            self._update_prompt()
            self._refresh_interaction_lock()
        elif kind == "project_file_export_finished":
            dialog = self.export_progress_dialog
            self.export_progress_dialog = None
            if dialog is not None and dialog.is_mounted:
                dialog.dismiss(True)
            self.project_file_exporting = False
            self._finish_project_progress()
            self._update_prompt()
            self._refresh_interaction_lock()
            if value is False:
                self.notify("项目文件导出失败", severity="error")
        elif kind == "diagnosis_export_finished":
            self.diagnosis_exporting = False
            self._update_prompt()
            self._refresh_interaction_lock()
            success, message = value
            if self.fatal_dialog is not None and self.fatal_dialog.is_mounted:
                self.fatal_dialog.finish_export(bool(success), str(message))
            elif success:
                self.notify(f"诊断信息已导出：{message}")
            else:
                self.notify(f"诊断信息导出失败：{message}", severity="error")
        elif kind == "project_loaded":
            self.project = Path(value) if value else self.project
            self.project_name = ""
            self._set_status(f"项目已加载：{self.project}")
        elif kind == "configuration":
            self.configuration_snapshot, self.configuration_read_only = value
            self._apply_client_preferences()
            if isinstance(self.screen, PreferencesDialog):
                self.screen.replace_snapshot(self.configuration_snapshot)
            self._refresh_menu_availability()
        elif kind == "configuration_cleared":
            self.configuration_snapshot = None
            self.configuration_read_only = False
            self._reset_client_preferences()
            self._refresh_menu_availability()
        elif kind == "configuration_saved":
            restart, restart_required = value
            if restart:
                if isinstance(self.screen, PreferencesDialog):
                    self.screen.dismiss()
                self.notify("项目设置已保存，正在重启游戏")
            elif restart_required:
                self.notify("项目设置已保存；部分更改将在重启后生效")
            else:
                self.notify("项目设置已应用")
        elif kind == "configuration_session_applied":
            if isinstance(self.screen, PreferencesDialog):
                self.screen.session_applied()
            self.notify("会话设置已应用；退出游戏后将丢失")
        elif kind == "configuration_save_failed":
            if isinstance(self.screen, PreferencesDialog):
                self.screen.save_failed(str(value))
            self.notify(str(value), severity="error")
        elif kind == "open_configuration":
            self.action_preferences()
        elif kind == "log":
            if isinstance(value, LogMessage):
                self._log(value.message, value.level, authoritative=value.authoritative)
            else:
                self._log(str(value))
        elif kind == "error":
            self._finish_project_progress()
            if isinstance(self.screen, PreferencesDialog) and self.screen.busy:
                self.screen.save_failed(str(value))
            self._log(str(value), LogLevel.ERROR)
            self.notify(str(value), title="RustyEra", severity="error", timeout=8)
        elif kind == "runtime_error":
            self._finish_project_progress()
            self.notify(str(value), title="RustyEra", severity="error", timeout=8)
        elif kind == "interaction_rejected":
            if self._wait_identity(value) == self._activated_wait:
                self.presentation.restore_buttons(self._pending_retired_buttons)
                self._pending_retired_buttons.clear()
                self._activated_wait = None
                self._queue_local_presentation_render()
                self._refresh_interaction_lock()
        elif kind == "runtime_fault":
            self._cancel_progress_loss_confirmation()
            self.snapshot_exporting = False
            self.active_wait = None
            self._activated_wait = None
            self.blocking_error = value.display()
            self.fault_logs = format_log_entries(self.logs)
            self._update_prompt()
            self._refresh_interaction_lock()
            if self.fatal_dialog is None or not self.fatal_dialog.is_mounted:
                self.fatal_dialog = FatalErrorDialog(self.blocking_error)
                self.push_screen(self.fatal_dialog)
        elif kind == "snapshot_restore_warning":
            self.notify(str(value), title="VM 快照恢复警告", severity="warning", timeout=12)
        elif kind == "debug_enabled":
            self._set_debug_enabled(bool(value))
        elif kind == "debug_stopped":
            source = value.get(3)
            self.debug_location = (
                f"{source.get(0)}:{source.get(4)}" if source and source.get(0) is not None else None
            )
            self.debug_paused = True
            self._update_prompt()
            self._refresh_interaction_lock()
        elif kind == "debug_response":
            self._handle_debug_response(value)
        elif kind == "exit_requested":
            if value == "重启":
                self.worker.send("restart")
            else:
                self.action_request_quit()
        elif kind == "shutdown_ready":
            self.worker.stop()
            if self.exit_pending:
                self.exit()
        elif kind == "worker_stopped":
            self._cancel_progress_loss_confirmation()
            self._finish_project_progress()
            if self.snapshot_exporting:
                self.snapshot_exporting = False
                self._update_prompt()
                self._refresh_interaction_lock()
            if self.diagnosis_exporting:
                self.diagnosis_exporting = False
                if self.fatal_dialog is not None and self.fatal_dialog.is_mounted:
                    self.fatal_dialog.finish_export(False, "Runtime worker 已停止")
                self._update_prompt()
                self._refresh_interaction_lock()
            if self.exit_pending:
                self.exit()
        return False

    def _set_active_wait(self, value: dict[int, Any] | None) -> None:
        wait_identity = self._wait_identity(value)
        if wait_identity != self._wait_identity(self.active_wait):
            self._pending_retired_buttons.clear()
            self._activated_wait = None
        self.active_wait = value
        self._update_prompt()
        self._refresh_interaction_lock()

    def _set_status(self, message: str) -> None:
        self._log(message)
        if message.startswith(self.PROJECT_PROGRESS_PREFIXES):
            self._begin_project_progress(message)
        elif message.startswith("脚本热重载完成"):
            self._finish_project_progress()
        if (
            self.project_progress_active
            or self.snapshot_exporting
            or self.project_file_exporting
            or self.presentation_rendering
        ):
            self._update_prompt()
        else:
            self.query_one("#prompt", Input).placeholder = message

    def _begin_project_progress(self, message: str, *, blocks_interaction: bool = True) -> None:
        if blocks_interaction:
            self._cancel_progress_loss_confirmation()
        self.project_progress_active = True
        self.project_progress_blocks_interaction = blocks_interaction
        self.project_progress_message = message
        if self.is_mounted:
            self._update_prompt()

    def _update_project_progress(self, stage: int, completed: int, total: int) -> None:
        if not 0 <= stage < len(self.PROJECT_PROGRESS_LABELS):
            return
        if stage == 0 and total <= 0:
            self._begin_project_progress("正在枚举项目文件…")
            return
        completed = max(0, min(completed, total)) if total > 0 else 0
        percent = min(100, completed * 100 // total) if total > 0 else 100
        filled = percent * 20 // 100
        bar = f"[{'█' * filled}{'░' * (20 - filled)}]"
        message = f"{self.PROJECT_PROGRESS_LABELS[stage]}：{completed}/{total} {bar} {percent}%"
        if self.project_file_exporting and self.export_progress_dialog is not None:
            self.export_progress_dialog.update_progress(message)
        self._begin_project_progress(
            message,
            blocks_interaction=stage != 9 or self.project_file_exporting,
        )

    def _finish_project_progress(self) -> None:
        self.project_progress_active = False
        self.project_progress_blocks_interaction = False
        self.project_progress_message = ""
        if self.is_mounted:
            self._update_prompt()

    def _log(
        self,
        message: str,
        level: LogLevel = LogLevel.INFO,
        *,
        authoritative: bool = False,
    ) -> None:
        entry = make_log_entry(message, level, authoritative=authoritative)
        self.logs.append(entry)
        if self.console_dialog is not None and self.console_dialog.is_mounted:
            if entry.level is LogLevel.DEBUG:
                self.console_dialog.write(entry.plain_text)

    def _update_prompt(self) -> None:
        prompt = self.query_one("#prompt", Input)
        if self.blocking_error is not None:
            self._set_prompt_state("prompt-error")
            prompt.disabled = True
            prompt.placeholder = self.blocking_error
            return
        if self.snapshot_exporting:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            prompt.placeholder = "VM 快照导出中……"
            return
        if self.project_file_exporting:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            prompt.placeholder = "项目文件导出中……"
            return
        if self.diagnosis_exporting:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            prompt.placeholder = "诊断信息导出中……"
            return
        if self.presentation_rendering:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            prompt.placeholder = "页面渲染中……"
            return
        if self.project_progress_active and self.project_progress_blocks_interaction:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            prompt.placeholder = self.project_progress_message
            return
        if self.debug_paused:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            location = self.debug_location or "当前位置不可用"
            prompt.placeholder = (
                f"单步暂停：{location}（F10 继续）" if self.single_step else f"调试暂停：{location}"
            )
            return
        prompt.disabled = self.active_wait is None
        if self.active_wait is None:
            self._set_prompt_state("prompt-running")
            prompt.placeholder = "Runtime 正在运行…"
            return
        kind_names = [
            "按 Enter 继续",
            "按任意键并回车",
            "输入整数",
            "输入文本",
            "继续",
            "输入值",
            "输入选项编号或点击按钮",
            "输入选项文本或点击按钮",
            "输入 type,result1,result2,result3,result4",
        ]
        kind = self.active_wait.get(1, 0)
        if kind in (2, 6):
            prompt_state = "prompt-number"
        elif kind == 0:
            prompt_state = "prompt-enter"
        else:
            prompt_state = "prompt-other"
        self._set_prompt_state(prompt_state)
        prompt.placeholder = kind_names[kind] if kind < len(kind_names) else "输入"
        prompt.focus()

    def _set_prompt_state(self, state_class: str) -> None:
        label = self.query_one("#prompt-label", Static)
        label.remove_class(*self.PROMPT_STATE_CLASSES)
        label.add_class(state_class)

    def _toggle_prompt_blink(self) -> None:
        try:
            label = self.query_one("#prompt-label", Static)
        except NoMatches:
            # A scheduled timer may fire after Textual has removed the composed
            # children while shutting down the app.
            return
        if label.has_class("prompt-running"):
            label.toggle_class("prompt-running-bright")
        else:
            label.remove_class("prompt-running-bright")

    def _game_interactions_blocked(self) -> bool:
        return (
            self.snapshot_exporting
            or self.project_file_exporting
            or self.diagnosis_exporting
            or self.presentation_rendering
            or self.blocking_error is not None
            or self.debug_paused
            or (
                self._activated_wait is not None
                and self._wait_identity(self.active_wait) == self._activated_wait
            )
        )

    def _debug_interactions_blocked(self) -> bool:
        return (
            not self.debug_enabled
            or not self._runtime_menu_actions_available()
            or self.snapshot_exporting
            or self.project_file_exporting
            or self.diagnosis_exporting
            or self.blocking_error is not None
        )

    def _refresh_interaction_lock(self) -> None:
        if not self.is_mounted:
            return
        viewport = self.query_one(GameViewport)
        if self._game_interactions_blocked():
            viewport.disable_interactions()
        else:
            viewport.enable_interactions()

    def _begin_presentation_render(self) -> None:
        self.presentation_rendering = True
        self._update_prompt()
        self._refresh_interaction_lock()

    def _finish_presentation_render(self, revision: int) -> None:
        if revision != self.presentation.revision:
            return
        self.presentation_rendering = False
        self._update_prompt()
        self._refresh_interaction_lock()

    def _queue_local_presentation_render(self) -> None:
        if not self._presentation_dirty:
            self._begin_presentation_render()
        self._presentation_dirty = True
        self._presentation_commit_ready = True

    def _set_debug_enabled(self, enabled: bool) -> None:
        self.debug_enabled = enabled
        self.query_one("#debug-toggle", Button).label = (
            "关闭调试模式" if enabled else "开启调试模式"
        )
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
        screen.query_one("#file-preferences", Button).disabled = (
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
        elif item_id == "file-reload-all":
            self.worker.send("reload_all")
        elif item_id == "file-reload-folder":
            self._choose_path("重新载入 Era 项目文件夹", "directory", "load_project")
        elif item_id == "file-reload-file":
            self._choose_path("重新载入脚本文件", "file", "reload_file")
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
        if self.configuration_snapshot is None:
            self.notify("Runtime 尚未提供项目配置", severity="warning")
            return
        self.push_screen(
            PreferencesDialog(self.configuration_snapshot, self.configuration_read_only)
        )

    def on_preferences_dialog_apply_requested(
        self, event: PreferencesDialog.ApplyRequested
    ) -> None:
        if event.restart and not event.changes:
            if isinstance(self.screen, PreferencesDialog):
                self.screen.dismiss()
            self.worker.send("restart")
            return
        self.worker.send("save_configuration", (event.changes, event.restart))

    def _apply_client_preferences(self) -> None:
        if self.configuration_snapshot is None or not self.is_mounted:
            return
        snapshot = self.configuration_snapshot
        screen = self.screen_stack[0]
        screen.query_one("#menu-bar").display = True
        viewport = screen.query_one(GameViewport)
        viewport.set_mouse_enabled(snapshot.effective_value("UseMouse", "YES") == "YES")
        viewport.set_replace_full_width_spaces(
            snapshot.effective_value("ReplaceFullWidthSpaces", "NO") == "YES"
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
        timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        return (self.project or Path.cwd()) / f"runtime_{timestamp}.snapshot"

    def _project_file_default_path(self) -> Path:
        title = self.project_name.strip() or self.project.name or "RustyEra项目"
        safe_title = diagnosis_project_name(title)
        return (self.project or Path.cwd()) / f"{safe_title}.reraproj"

    def _log_default_path(self, now: datetime | None = None) -> Path:
        timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        return (self.project or Path.cwd()) / f"log_{timestamp}.log"

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
        self._update_prompt()
        self._refresh_interaction_lock()
        if self.fatal_dialog is not None and self.fatal_dialog.is_mounted:
            self.fatal_dialog.set_exporting()
        self.fault_logs = format_log_entries(self.logs)
        self.worker.send(
            "export_diagnosis",
            (path, self.fault_logs, self._diagnosis_project_name()),
        )

    def _open_diagnosis_export_dialog(self) -> None:
        initial = diagnosis_default_path(
            self.project or Path.cwd(),
            project_name=self._diagnosis_project_name(),
        )
        self.push_screen(
            PathDialog("导出诊断信息", "save", initial),
            self._start_diagnosis_export,
        )

    def _capture_project_name(self) -> None:
        title = self.presentation.title.strip()
        if not self.project_name and title and title != self.TITLE:
            self.project_name = title

    def _diagnosis_project_name(self) -> str:
        if self.project_name:
            return self.project_name
        return "" if self.presentation.title == self.TITLE else self.presentation.title

    def _help_action(self, item_id: str) -> None:
        if item_id == "help-export-diagnosis":
            self._open_diagnosis_export_dialog()
        elif item_id == "help-about":
            self.push_screen(AboutDialog(frontend_version(), CORE_VERSION))

    def _debug_action(self, item_id: str) -> None:
        if self.snapshot_exporting and item_id != "debug-logs":
            self.notify("VM 快照导出完成前不能执行调试操作", severity="warning")
            return
        if item_id == "debug-toggle":
            self.worker.send("debug_disable" if self.debug_enabled else "debug_enable")
        elif item_id == "debug-console" and self.debug_enabled:
            self.console_dialog = DebugConsoleDialog()
            self.push_screen(
                self.console_dialog,
                lambda _result: self.worker.send("debug_surface_closed", "console"),
            )
        elif item_id == "debug-variables" and self.debug_enabled:
            self.variable_dialog = VariableDialog()
            self.push_screen(
                self.variable_dialog,
                lambda _result: self.worker.send("debug_surface_closed", "variables"),
            )
            self.worker.send("debug_action", ("variables", None))
        elif item_id == "debug-stack" and self.debug_enabled:
            self.stack_dialog = StackDialog()
            self.push_screen(
                self.stack_dialog,
                lambda _result: self.worker.send("debug_surface_closed", "stack"),
            )
        elif item_id == "debug-step-toggle" and self.debug_enabled:
            self.single_step = not self.single_step
            self.query_one("#debug-step-toggle", Button).label = (
                "关闭单步运行" if self.single_step else "开启单步运行"
            )
            self.worker.send("debug_single_step", self.single_step)
        elif item_id == "debug-logs":
            self.push_screen(LogDialog(self.logs))

    def _handle_debug_response(self, value: tuple[str, int, list[Any]]) -> None:
        pending, response_tag, fields = value
        if (
            response_tag == 1
            and fields
            and self.variable_dialog
            and self.variable_dialog.is_mounted
        ):
            self.variable_dialog.set_variables(fields[0])
        elif (
            response_tag == 2
            and fields
            and self.variable_dialog
            and self.variable_dialog.is_mounted
        ):
            self.variable_dialog.set_value(fields[0])
        elif response_tag == 5 and fields and self.stack_dialog and self.stack_dialog.is_mounted:
            fiber_id, next_cursor = self.stack_dialog.set_fibers(fields[0])
            if fiber_id is not None:
                self.worker.send("debug_action", ("call_stack", fiber_id))
            elif next_cursor is not None:
                self.worker.send("debug_action", ("fibers", next_cursor))
        elif response_tag == 6 and fields and self.stack_dialog and self.stack_dialog.is_mounted:
            self.stack_dialog.set_frames(fields[0])
        elif (
            response_tag == 8 and fields and self.console_dialog and self.console_dialog.is_mounted
        ):
            outcome = fields[0]
            for line in outcome.get(2, []):
                self.console_dialog.write(line)
            if outcome.get(1) is not None:
                self.console_dialog.write(f"=> {outcome[1]!r}")
            for diagnostic in outcome.get(5, []):
                self.console_dialog.write(f"{diagnostic.get(0)}: {diagnostic.get(1)}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt" or self._game_interactions_blocked():
            return
        if self._submit_active_wait("submit_text", event.value):
            event.input.value = ""

    def on_key(self, event: events.Key) -> None:
        if (
            len(self.screen_stack) != 1
            or self._game_interactions_blocked()
            or self.active_wait is None
            or not is_message_wait(self.active_wait)
            or self.active_wait.get(1) != 1
            or event.key in {"alt", "ctrl", "meta", "shift", "super"}
            or "+" in event.key
        ):
            return
        if self._submit_active_wait("submit_text", event.character or ""):
            event.prevent_default()
            event.stop()

    def on_stack_dialog_ready(self, _event: StackDialog.Ready) -> None:
        if not self._debug_interactions_blocked():
            self.worker.send("debug_action", ("fibers", None))

    def on_game_line_activated(self, event: GameLine.Activated) -> None:
        wait_identity = self._wait_identity(self.active_wait)
        if (
            self._game_interactions_blocked()
            or wait_identity is None
            or wait_identity == self._activated_wait
            or not self.presentation.has_enabled_button(event.token)
        ):
            return
        if event.title and event.title.startswith("Delete "):
            save_name = event.title.removeprefix("Delete ")
            self.push_screen(
                ConfirmDialog(
                    "删除存档",
                    f"确定要永久删除存档 {save_name} 吗？",
                    "删除",
                ),
                lambda confirmed: self._activate_button(event.token) if confirmed else None,
            )
            return
        self._activate_button(event.token)

    def _activate_button(self, token: dict[int, Any]) -> None:
        wait_identity = self._wait_identity(self.active_wait)
        if (
            self._game_interactions_blocked()
            or wait_identity is None
            or wait_identity == self._activated_wait
            or not self.presentation.has_enabled_button(token)
        ):
            return
        self._activated_wait = wait_identity
        self._pending_retired_buttons = self.presentation.retire_enabled_buttons()
        self._queue_local_presentation_render()
        self.query_one(GameViewport).disable_interactions()
        self.worker.send("activate", token)

    def _submit_active_wait(self, command: str, value: Any) -> bool:
        wait_identity = self._wait_identity(self.active_wait)
        if (
            self._game_interactions_blocked()
            or wait_identity is None
            or wait_identity == self._activated_wait
        ):
            return False
        self._activated_wait = wait_identity
        self._pending_retired_buttons = self.presentation.retire_enabled_buttons()
        self._queue_local_presentation_render()
        self.query_one(GameViewport).disable_interactions()
        self.worker.send(command, value)
        return True

    @staticmethod
    def _wait_identity(wait: dict[int, Any] | None) -> tuple[int, Any] | None:
        if wait is None:
            return None
        return wait[0], wait.get(11)

    def on_game_viewport_continue_requested(self, _event: GameViewport.ContinueRequested) -> None:
        if (
            not self._game_interactions_blocked()
            and self.active_wait is not None
            and is_message_wait(self.active_wait)
        ):
            self._submit_active_wait("submit_text", "")

    def on_game_viewport_skip_message_requested(
        self, _event: GameViewport.SkipMessageRequested
    ) -> None:
        if (
            not self._game_interactions_blocked()
            and self.active_wait is not None
            and is_message_skip_wait(self.active_wait)
        ):
            self._submit_active_wait("skip_message_waits", None)

    def on_game_viewport_horizontal_overflow_changed(
        self, event: GameViewport.HorizontalOverflowChanged
    ) -> None:
        self.query_one("#separator-line").display = not event.visible

    def on_debug_console_dialog_submitted(self, event: DebugConsoleDialog.Submitted) -> None:
        if self._debug_interactions_blocked():
            return
        action = "console_execute" if event.execute else "console_evaluate"
        self.worker.send("debug_action", (action, event.source))

    def on_variable_refresh(self, _event: VariableRefresh) -> None:
        if not self._debug_interactions_blocked():
            self.worker.send("debug_action", ("variables", None))

    def on_variable_dialog_read_requested(self, event: VariableDialog.ReadRequested) -> None:
        if not self._debug_interactions_blocked():
            self.worker.send("debug_action", ("read_variable", event.descriptor))

    def on_fatal_error_dialog_action(self, event: FatalErrorDialog.Action) -> None:
        if self.diagnosis_exporting:
            return
        if event.action == "export":
            self._open_diagnosis_export_dialog()
            return
        dialog = self.fatal_dialog
        self.fatal_dialog = None
        if dialog is not None and dialog.is_mounted:
            dialog.dismiss(None)
        if event.action == "title":
            self.worker.send("return_title")
        elif event.action == "recompile":
            self.worker.send("restart_recompile")
        elif event.action == "exit":
            self.action_request_quit()

    def on_log_dialog_action(self, event: LogDialog.Action) -> None:
        if event.action != "export":
            return
        self.push_screen(
            PathDialog("导出日志", "save", self._log_default_path()),
            lambda path: self._export_logs(path, event.contents),
        )

    def on_resize(self, _event: events.Resize) -> None:
        self._schedule_viewport_projection()

    def _schedule_viewport_projection(self) -> None:
        if not self._projection_refresh_scheduled:
            self._projection_refresh_scheduled = True
            self.call_after_refresh(self._send_viewport_projection)

    def _send_viewport_projection(self) -> None:
        self._projection_refresh_scheduled = False
        if not self.is_mounted:
            return
        viewport = self.query_one(GameViewport)
        self._send_projection(*viewport.content_dimensions)

    def _send_projection(self, width: int, height: int) -> None:
        # Each observation is bound to the currently applied presentation revision. The
        # runtime therefore treats this as a causal observation revision, even when only
        # presentation content (rather than terminal geometry) changed.
        self.environment_revision += 1
        self.worker.send(
            "projection",
            (
                max(1, width),
                max(1, height),
                self.environment_revision,
                self.presentation.revision,
            ),
        )

    def action_input_undo(self) -> None:
        if self.input_undo_token is not None:
            self._submit_active_wait("input_undo", self.input_undo_token)

    def action_debug_step(self) -> None:
        if (
            self._runtime_menu_actions_available()
            and self.debug_enabled
            and self.single_step
            and self.debug_paused
        ):
            self.debug_paused = False
            self.debug_location = None
            self._update_prompt()
            self._refresh_interaction_lock()
            self.worker.send("debug_step")

    def action_request_quit(self) -> None:
        if self.exit_pending:
            return
        self.exit_pending = True
        self._set_status("正在正常关闭 Runtime…")
        self.worker.send("shutdown")
        self.set_timer(3.0, self._force_exit)

    def _force_exit(self) -> None:
        if self.exit_pending:
            self.worker.stop()
            self.exit()
