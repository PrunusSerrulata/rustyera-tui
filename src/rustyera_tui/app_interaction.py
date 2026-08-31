"""Private RustyEraTui responsibilities extracted from the app facade."""

from __future__ import annotations

from typing import Any

from textual import events
from textual.widgets import Button, Input

from .dialogs import (
    ConfirmDialog,
    DebugConsoleDialog,
    FatalErrorDialog,
    LogDialog,
    PathDialog,
    StackDialog,
    VariableDialog,
    VariableRefresh,
)
from .dialogs_debug import MAX_DEBUG_VALUE_BYTES
from .input_policy import is_message_skip_wait, is_message_wait
from .app_viewport import _ViewportProjectionMixin
from .text_budget import bounded_repr, truncate_utf8
from .widgets import GameLine, GameViewport


class _InteractionMixin(_ViewportProjectionMixin):
    def _debug_action(self, item_id: str) -> None:
        if (self.input_replay_exporting or self.snapshot_exporting) and item_id != "debug-logs":
            self.notify("状态导出完成前不能执行调试操作", severity="warning")
            return
        if item_id == "debug-toggle":
            self.worker.send("debug_disable" if self.debug_enabled else "debug_enable")
        elif item_id == "debug-console" and self.debug_enabled:
            dialog = DebugConsoleDialog()
            self.console_dialog = dialog
            self.push_screen(
                dialog,
                lambda _result: self._finish_debug_surface("console_dialog", dialog, "console"),
            )
        elif item_id == "debug-variables" and self.debug_enabled:
            dialog = VariableDialog()
            self.variable_dialog = dialog
            self.push_screen(
                dialog,
                lambda _result: self._finish_debug_surface("variable_dialog", dialog, "variables"),
            )
            self.worker.send("debug_action", ("variables", None))
        elif item_id == "debug-stack" and self.debug_enabled:
            dialog = StackDialog()
            self.stack_dialog = dialog
            self.push_screen(
                dialog,
                lambda _result: self._finish_debug_surface("stack_dialog", dialog, "stack"),
            )
        elif item_id == "debug-step-toggle" and self.debug_enabled:
            self.single_step = not self.single_step
            self.query_one("#debug-step-toggle", Button).label = (
                "关闭单步运行" if self.single_step else "开启单步运行"
            )
            self.worker.send("debug_single_step", self.single_step)
        elif item_id == "debug-logs":
            self.push_screen(LogDialog(self.logs))

    def _finish_debug_surface(self, attribute: str, dialog: object, owner: str) -> None:
        if getattr(self, attribute) is not dialog:
            return
        setattr(self, attribute, None)
        self.worker.send("debug_surface_closed", owner)

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
                self.console_dialog.write(
                    f"=> {bounded_repr(outcome[1], MAX_DEBUG_VALUE_BYTES - 3)}"
                )
            for diagnostic in outcome.get(5, []):
                category = diagnostic.get(0)
                message = diagnostic.get(1)
                self.console_dialog.write(
                    f"{truncate_utf8(category, MAX_DEBUG_VALUE_BYTES // 4) if isinstance(category, str) else bounded_repr(category, MAX_DEBUG_VALUE_BYTES // 4)}: "
                    f"{truncate_utf8(message, MAX_DEBUG_VALUE_BYTES * 3 // 4) if isinstance(message, str) else bounded_repr(message, MAX_DEBUG_VALUE_BYTES * 3 // 4)}"
                )

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
        self._pending_retired_interaction_boundary = (
            self.presentation.retire_presented_interactions()
        )
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
        self._pending_retired_interaction_boundary = (
            self.presentation.retire_presented_interactions()
        )
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
