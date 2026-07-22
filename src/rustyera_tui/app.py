"""Textual application shell for the RustyEra runtime frontend."""

from __future__ import annotations

import queue
from datetime import datetime
from pathlib import Path
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Rule, Static

from .dialogs import (
    DebugConsoleDialog,
    LogDialog,
    PathDialog,
    StackDialog,
    VariableDialog,
    VariableRefresh,
)
from .presentation import PresentationModel
from .protocol_text import DEBUG_STOP_REASONS, RUNTIME_PHASES, enum_text, variant_enum_text
from .runtime import FrontendEvent, RuntimeWorker
from .widgets import GameLine, GameViewport


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
        Binding("f10", "debug_step", "单步"),
        Binding("escape", "close_menus", "关闭菜单", show=False),
    ]

    FILE_ITEMS = (
        ("file-restart", "重启"),
        ("file-title", "返回标题画面"),
        ("file-reload-all", "重新载入所有脚本"),
        ("file-reload-folder", "重新载入文件夹..."),
        ("file-reload-file", "重新载入脚本文件..."),
        ("file-export-snapshot", "导出当前VM快照..."),
        ("file-restore-snapshot", "恢复VM快照..."),
        ("file-exit", "退出"),
    )

    def __init__(self, project: Path | None, runtime_library: Path | None) -> None:
        super().__init__()
        self.project = project.expanduser() if project else None
        self.runtime_library = runtime_library
        self.worker = RuntimeWorker(runtime_library, self.project)
        self.presentation = PresentationModel()
        self.active_wait: dict[int, Any] | None = None
        self.blocking_error: str | None = None
        self.input_undo_token: dict[int, Any] | None = None
        self.logs: list[str] = []
        self.debug_enabled = False
        self.single_step = False
        self.environment_revision = 0
        self.exit_pending = False
        self.variable_dialog: VariableDialog | None = None
        self.stack_dialog: StackDialog | None = None
        self.console_dialog: DebugConsoleDialog | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="app-root"):
            with Horizontal(id="menu-bar"):
                yield Button("文件", id="menu-file", classes="menu-button")
                yield Button("调试", id="menu-debug", classes="menu-button")
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

    def on_mount(self) -> None:
        self.worker.start()
        self.set_interval(0.03, self._drain_worker_events)
        self.set_interval(0.5, self._toggle_prompt_blink)
        self._update_prompt()
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        if self.worker.is_alive():
            self.worker.stop()
            self.worker.join(timeout=2)

    async def _drain_worker_events(self) -> None:
        presentation_dirty = False
        for _ in range(1000):
            try:
                event = self.worker.events.get_nowait()
            except queue.Empty:
                break
            dirty = self._handle_worker_event(event)
            presentation_dirty = presentation_dirty or dirty
        if presentation_dirty:
            viewport = self.query_one(GameViewport)
            await viewport.set_lines(self.presentation.lines)
            self.title = self.presentation.title or self.TITLE
            viewport.styles.background = self.presentation.background
            self._send_projection(viewport.size.width, viewport.size.height)

    def _handle_worker_event(self, event: FrontendEvent) -> bool:
        kind, value = event.kind, event.value
        if kind == "presentation_snapshot":
            self.presentation.apply_snapshot(value)
            return True
        if kind == "presentation_delta":
            try:
                self.presentation.apply_delta(value)
            except ValueError as error:
                self._log(str(error))
            return True
        if kind == "wait":
            self.active_wait = value
            self._update_prompt()
        elif kind == "input_undo":
            self.input_undo_token = value.get(4) if value.get(0) else None
        elif kind == "text_box":
            prompt = self.query_one("#prompt", Input)
            if not prompt.value:
                prompt.value = value
        elif kind == "phase":
            if value != 11 and self.blocking_error is not None:
                self.blocking_error = None
                self._update_prompt()
            phase = enum_text(value, RUNTIME_PHASES, "RuntimePhase")
            self._log(f"Runtime phase -> {phase}")
        elif kind == "status":
            self._set_status(str(value))
        elif kind == "project_loaded":
            self.project = Path(value) if value else self.project
            self._set_status(f"项目已加载：{self.project}")
        elif kind == "log":
            self._log(str(value))
        elif kind == "error":
            self._log(f"ERROR: {value}")
            self.notify(str(value), title="RustyEra", severity="error", timeout=8)
        elif kind == "runtime_fault":
            self.active_wait = None
            self.blocking_error = value.display()
            self._log(f"ERROR: {self.blocking_error}")
            self._update_prompt()
            self.notify(self.blocking_error, title="RustyEra", severity="error", timeout=12)
        elif kind == "debug_enabled":
            self._set_debug_enabled(bool(value))
        elif kind == "debug_stopped":
            reason = variant_enum_text(value.get(1), DEBUG_STOP_REASONS, "StopReason")
            self._set_status(f"调试暂停：{reason}（F10 单步）")
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
            if self.exit_pending:
                self.exit()
        return False

    def _set_status(self, message: str) -> None:
        self._log(message)
        self.query_one("#prompt", Input).placeholder = message

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > 10_000:
            del self.logs[: len(self.logs) - 10_000]
        if self.console_dialog is not None and self.console_dialog.is_mounted:
            if message.startswith("DEBUG:"):
                self.console_dialog.write(message)

    def _update_prompt(self) -> None:
        prompt = self.query_one("#prompt", Input)
        if self.blocking_error is not None:
            self._set_prompt_state("prompt-error")
            prompt.disabled = True
            prompt.placeholder = self.blocking_error
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
        label = self.query_one("#prompt-label", Static)
        if label.has_class("prompt-running"):
            label.toggle_class("prompt-running-bright")
        else:
            label.remove_class("prompt-running-bright")

    def _set_debug_enabled(self, enabled: bool) -> None:
        self.debug_enabled = enabled
        self.query_one("#debug-toggle", Button).label = (
            "关闭调试模式" if enabled else "开启调试模式"
        )
        for item_id in ("debug-console", "debug-variables", "debug-stack", "debug-step-toggle"):
            self.query_one(f"#{item_id}", Button).disabled = not enabled
        if not enabled:
            self.single_step = False
            self.query_one("#debug-step-toggle", Button).label = "开启单步运行"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        item_id = event.button.id or ""
        if item_id == "menu-file":
            self._toggle_menu("file")
            return
        if item_id == "menu-debug":
            self._toggle_menu("debug")
            return
        if item_id.startswith("file-"):
            self.action_close_menus()
            self._file_action(item_id)
        elif item_id.startswith("debug-"):
            self.action_close_menus()
            self._debug_action(item_id)

    def _toggle_menu(self, name: str) -> None:
        selected = self.query_one(f"#{name}-menu")
        other = self.query_one("#debug-menu" if name == "file" else "#file-menu")
        other.remove_class("visible")
        selected.toggle_class("visible")

    def action_close_menus(self) -> None:
        self.query_one("#file-menu").remove_class("visible")
        self.query_one("#debug-menu").remove_class("visible")

    def _file_action(self, item_id: str) -> None:
        if item_id == "file-restart":
            self.worker.send("restart")
        elif item_id == "file-title":
            self.worker.send("return_title")
        elif item_id == "file-reload-all":
            self.worker.send("reload_all")
        elif item_id == "file-reload-folder":
            self._choose_path("重新载入 Era 项目文件夹", "directory", "load_project")
        elif item_id == "file-reload-file":
            self._choose_path("重新载入脚本文件", "file", "reload_file")
        elif item_id == "file-export-snapshot":
            initial = (self.project or Path.cwd()) / "runtime.snapshot"
            self.push_screen(
                PathDialog("导出当前 VM 快照", "save", initial),
                lambda path: path and self.worker.send("export_snapshot", path),
            )
        elif item_id == "file-restore-snapshot":
            self._choose_path("恢复 VM 快照", "file", "restore_snapshot")
        elif item_id == "file-exit":
            self.action_request_quit()

    def _choose_path(self, title: str, mode: str, command: str) -> None:
        initial = self.project or Path.cwd()
        self.push_screen(
            PathDialog(title, mode, initial),
            lambda path: path and self.worker.send(command, path),
        )

    def _debug_action(self, item_id: str) -> None:
        if item_id == "debug-toggle":
            self.worker.send("debug_disable" if self.debug_enabled else "debug_enable")
        elif item_id == "debug-console" and self.debug_enabled:
            self.console_dialog = DebugConsoleDialog()
            self.push_screen(self.console_dialog)
        elif item_id == "debug-variables" and self.debug_enabled:
            self.variable_dialog = VariableDialog()
            self.push_screen(self.variable_dialog)
            self.worker.send("debug_action", ("variables", None))
        elif item_id == "debug-stack" and self.debug_enabled:
            self.stack_dialog = StackDialog()
            self.push_screen(self.stack_dialog)
            self.worker.send("debug_action", ("fibers", None))
        elif item_id == "debug-step-toggle" and self.debug_enabled:
            self.single_step = not self.single_step
            self.query_one("#debug-step-toggle", Button).label = (
                "关闭单步运行" if self.single_step else "开启单步运行"
            )
            if self.single_step:
                self.worker.send("debug_action", ("pause_only", None))
        elif item_id == "debug-logs":
            self.push_screen(LogDialog(list(self.logs)))

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
            fiber_id = self.stack_dialog.set_fibers(fields[0])
            if fiber_id is not None:
                self.worker.send("debug_action", ("call_stack", fiber_id))
        elif response_tag == 6 and fields and self.stack_dialog and self.stack_dialog.is_mounted:
            target = self.stack_dialog.set_frames(fields[0])
            if target is not None:
                self.worker.send("debug_action", ("operand_stack", target))
        elif response_tag == 7 and fields and self.stack_dialog and self.stack_dialog.is_mounted:
            self.stack_dialog.set_operands(fields[0])
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
        elif response_tag == 0 and pending:
            self._log(f"DEBUG: {pending} accepted")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        self.worker.send("submit_text", event.value)
        event.input.value = ""

    def on_game_line_activated(self, event: GameLine.Activated) -> None:
        self.worker.send("activate", event.token)

    def on_game_viewport_continue_requested(self, _event: GameViewport.ContinueRequested) -> None:
        if self.active_wait is not None and self.active_wait.get(1) == 0:
            self.worker.send("submit_text", "")

    def on_game_viewport_skip_enter_requested(
        self, _event: GameViewport.SkipEnterRequested
    ) -> None:
        if self.active_wait is not None and self.active_wait.get(1) == 0:
            self.worker.send("skip_enter_waits")

    def on_game_viewport_horizontal_scrollbar_changed(
        self, event: GameViewport.HorizontalScrollbarChanged
    ) -> None:
        self.query_one("#separator-line").display = not event.visible

    def on_debug_console_dialog_submitted(self, event: DebugConsoleDialog.Submitted) -> None:
        action = "console_execute" if event.execute else "console_evaluate"
        self.worker.send("debug_action", (action, event.source))

    def on_variable_refresh(self, _event: VariableRefresh) -> None:
        self.worker.send("debug_action", ("variables", None))

    def on_variable_dialog_read_requested(self, event: VariableDialog.ReadRequested) -> None:
        self.worker.send("debug_action", ("read_variable", event.descriptor))

    def on_resize(self, event: events.Resize) -> None:
        self._send_projection(event.size.width, event.size.height - 3)

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
            self.worker.send("input_undo", self.input_undo_token)

    def action_debug_step(self) -> None:
        if self.debug_enabled and self.single_step:
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
