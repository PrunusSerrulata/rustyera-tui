"""Textual app shell with stable public class identity."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Input, ProgressBar, Rule, Static

from .configuration import ConfigurationSnapshot
from .client_preferences import LoadedPreferences, PreferenceValues, global_preferences_path
from .dialogs import (
    ConfirmDialog,
    DebugConsoleDialog,
    ExportProgressDialog,
    FatalErrorDialog,
    StackDialog,
    VariableDialog,
)
from .log_model import BudgetedLogEntries, LogLevel, make_log_entry
from .presentation import PresentationModel
from .runtime import RuntimeWorker
from .runtime_types import GameInformation
from .version import CORE_VERSION
from .widgets import GameViewport

from .app_interaction import _InteractionMixin
from .app_menu import _MenuAndExportMixin
from .app_worker import _WorkerEventMixin
from .app_progress import (
    PROJECT_PROGRESS_LABELS as _PROJECT_PROGRESS_LABELS,
    PROJECT_PROGRESS_PREFIXES as _PROJECT_PROGRESS_PREFIXES,
    format_project_progress,
)


class RustyEraTui(_WorkerEventMixin, _MenuAndExportMixin, _InteractionMixin, App[None]):
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
        ("file-export-project", "导出全量项目文件…"),
        ("file-restart", "重新开始"),
        ("file-title", "返回标题"),
        ("file-reload-all", "重新加载全部脚本"),
        ("file-reload-folder", "重新加载文件夹…"),
        ("file-reload-file", "重新加载单个脚本…"),
        ("file-export-input-replay", "导出操作序列…"),
        ("file-export-snapshot", "导出 VM 快照…"),
        ("file-restore-snapshot", "恢复 VM 快照…"),
        ("file-project-settings", "项目设置…"),
        ("file-preferences", "偏好设置…"),
        ("file-exit", "退出"),
    )
    FILE_SEPARATORS = frozenset({"file-export-input-replay", "file-preferences", "file-exit"})
    GAME_READY_PHASES = {4, 5, 6, 7, 10, 11}
    GAME_FILE_ITEMS = (
        "file-restart",
        "file-title",
        "file-reload-all",
        "file-reload-folder",
        "file-reload-file",
        "file-export-project",
        "file-export-input-replay",
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
    PROJECT_PROGRESS_PREFIXES = _PROJECT_PROGRESS_PREFIXES
    PROJECT_PROGRESS_LABELS = _PROJECT_PROGRESS_LABELS
    DIAGNOSIS_PROGRESS_LABELS = {
        "waiting": "正在准备诊断信息",
        "input_replay": "正在导出输入回放",
        "vm_snapshot": "正在导出 VM 快照",
        "project_scanning": "正在读取项目文件",
        "project_preparing": "正在准备全量项目文件",
        "project_packaging": "正在打包全量项目文件",
        "project_transfer": "正在传输全量项目文件",
        "archive": "正在写入诊断归档",
    }

    def __init__(
        self,
        resource_directory: Path | None,
        runtime_library: Path | None,
        project_file: Path | None = None,
        worker: RuntimeWorker | None = None,
    ) -> None:
        super().__init__()
        self.project = (
            resource_directory.expanduser()
            if resource_directory
            else project_file.expanduser().parent
            if project_file
            else Path.cwd()
        )
        self.project_file = project_file.expanduser() if project_file else None
        # The worker owns preference I/O and replaces this placeholder via an event.
        self.global_preferences = LoadedPreferences(global_preferences_path(), PreferenceValues({}))
        self.project_preferences = None
        self.runtime_library = runtime_library
        self.worker = worker or RuntimeWorker(
            runtime_library, resource_directory, initial_project_file=project_file
        )
        self.presentation = PresentationModel()
        self.game_information = GameInformation()
        self.core_version = CORE_VERSION
        self.active_wait: dict[int, Any] | None = None
        self._activated_wait: tuple[int, Any] | None = None
        self._pending_retired_interaction_boundary: int | None = None
        self.blocking_error: str | None = None
        self.input_undo_token: dict[int, Any] | None = None
        self.logs: BudgetedLogEntries = BudgetedLogEntries()
        self.debug_enabled = False
        self.single_step = False
        self.debug_paused = False
        self.debug_location: str | None = None
        self.runtime_phase = 0
        self.environment_revision = 0
        self.exit_pending = False
        self.input_replay_exporting = False
        self.snapshot_exporting = False
        self.project_file_exporting = False
        self.export_progress_dialog: ExportProgressDialog | None = None
        self.diagnosis_exporting = False
        self.diagnosis_export_at_fault = False
        self.presentation_rendering = False
        self._presentation_dirty = False
        self._presentation_commit_ready = False
        self._projection_refresh_scheduled = False
        self._worker_event_notification_lock = threading.Lock()
        self._worker_event_notification_pending = False
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
            with Vertical(id="diagnosis-progress"):
                yield Static("正在准备诊断信息…", id="diagnosis-progress-label", markup=False)
                yield ProgressBar(total=None, show_eta=False, id="diagnosis-progress-bar")
            with Horizontal(id="prompt-row"):
                yield Static("> ", id="prompt-label", classes="prompt-running")
                yield Input(placeholder="等待 Runtime…", id="prompt", disabled=True)
        with Vertical(id="file-menu", classes="dropdown"):
            for item_id, label in self.FILE_ITEMS:
                if item_id in self.FILE_SEPARATORS:
                    yield Rule(classes="menu-separator")
                yield Button(label, id=item_id, classes="menu-item")
        with Vertical(id="debug-menu", classes="dropdown"):
            yield Button("启用调试", id="debug-toggle", classes="menu-item")
            yield Rule(classes="menu-separator")
            yield Button("控制台…", id="debug-console", classes="menu-item", disabled=True)
            yield Button("变量查看器…", id="debug-variables", classes="menu-item", disabled=True)
            yield Button("Fibers / 调用栈…", id="debug-stack", classes="menu-item", disabled=True)
            yield Button("开启单步运行", id="debug-step-toggle", classes="menu-item", disabled=True)
            yield Rule(classes="menu-separator")
            yield Button("日志…", id="debug-logs", classes="menu-item")
        with Vertical(id="help-menu", classes="dropdown"):
            yield Button("导出诊断信息…", id="help-export-diagnosis", classes="menu-item")
            yield Rule(classes="menu-separator")
            yield Button("关于…", id="help-about", classes="menu-item")

    def on_mount(self) -> None:
        set_notifier = getattr(self.worker, "set_event_notifier", None)
        if set_notifier is not None:
            set_notifier(self._notify_worker_events)
        if self.worker.ident is None:
            self.worker.start()
        self.set_interval(0.03, self._drain_worker_events)
        self.set_interval(0.5, self._toggle_prompt_blink)
        self._update_prompt()
        self._refresh_menu_availability()
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        set_notifier = getattr(self.worker, "set_event_notifier", None)
        if set_notifier is not None:
            set_notifier(None)
        self.worker.shutdown()

    def _set_active_wait(self, value: dict[int, Any] | None) -> None:
        wait_identity = self._wait_identity(value)
        if wait_identity != self._wait_identity(self.active_wait):
            self._activated_wait = None
            boundary = self._pending_retired_interaction_boundary
            if wait_identity is not None and boundary is not None:
                restored = self.presentation.restore_submitted_interaction_boundary(boundary)
                self._pending_retired_interaction_boundary = None
                if restored:
                    self._queue_local_presentation_render()
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
            or self.input_replay_exporting
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
        progress = format_project_progress(
            stage,
            completed,
            total,
            project_file_exporting=self.project_file_exporting,
            labels=self.PROJECT_PROGRESS_LABELS,
        )
        if progress is None:
            return
        if (
            progress.updates_export_dialog
            and self.project_file_exporting
            and self.export_progress_dialog is not None
        ):
            self.export_progress_dialog.update_progress(progress.message)
        self._begin_project_progress(
            progress.message,
            blocks_interaction=progress.blocks_interaction,
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
        if self.input_replay_exporting:
            self._set_prompt_state("prompt-running")
            prompt.disabled = True
            prompt.placeholder = "操作序列导出中……"
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
            self.input_replay_exporting
            or self.snapshot_exporting
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
            or self.input_replay_exporting
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
        self._mark_presentation_dirty()
        self._presentation_commit_ready = True
