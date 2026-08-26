"""Private RustyEraTui responsibilities extracted from the app facade."""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input

from .dialogs import FatalErrorDialog, PreferencesDialog, ProjectSettingsDialog
from .log_model import LogLevel, LogMessage, format_log_entries
from .presentation import PresentationModel
from .runtime import DiagnosisProgress, FrontendEvent, PresentationBatch
from .runtime_types import GameInformation
from .version import CORE_REVISION
from .widgets import GameViewport


class WorkerEventsAvailable(Message):
    """Advisory, coalesced notification that the runtime queue has work."""


class _WorkerEventMixin:
    def _notify_worker_events(self) -> None:
        """Coalesce worker-thread publications into one prompt UI-loop drain."""

        with self._worker_event_notification_lock:
            if self._worker_event_notification_pending:
                return
            self._worker_event_notification_pending = True
        try:
            # post_message is thread-safe and non-blocking. In particular, the runtime worker must
            # never wait for Textual's loop while that loop may be joining the worker on shutdown.
            posted = self.post_message(WorkerEventsAvailable())
        except RuntimeError:
            posted = False
        if not posted:
            with self._worker_event_notification_lock:
                self._worker_event_notification_pending = False

    def on_worker_events_available(self, _message: WorkerEventsAvailable) -> None:
        self.call_next(self._drain_notified_worker_events)

    async def _drain_notified_worker_events(self) -> None:
        await self._drain_worker_events()
        with self._worker_event_notification_lock:
            self._worker_event_notification_pending = False
            reschedule = not self.worker.events.empty()
            if reschedule:
                self._worker_event_notification_pending = True
        if reschedule:
            self.call_next(self._drain_notified_worker_events)

    async def _drain_worker_events(self) -> None:
        queue_exhausted = False
        for _ in range(1000):
            try:
                event = self.worker.events.get_nowait()
            except queue.Empty:
                queue_exhausted = True
                break
            dirty = self._handle_worker_event(event)
            if dirty:
                self._mark_presentation_dirty()
            if (
                event.kind in {"session_reset", "game_state_reset"}
                and self._presentation_dirty
                and self._presentation_commit_ready
            ):
                # Release the outgoing Textual widget tree before draining any new-game
                # presentation batches that may already be queued behind the reset.
                await self._commit_presentation()
        if queue_exhausted and self._presentation_dirty and self._presentation_commit_ready:
            await self._commit_presentation()

    def _mark_presentation_dirty(self) -> None:
        """Lock the outgoing interaction surface at the first pending projection change."""

        if self._presentation_dirty:
            return
        self._begin_presentation_render()
        self._presentation_dirty = True

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
                button_generation=self.presentation.button_generation,
                retired_interaction_sequence=(self.presentation.retired_interaction_sequence),
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
        if kind == "session_reset":
            self._reset_session_state()
            return True
        if kind == "game_state_reset":
            self._reset_game_state_projection(int(value))
            return True
        if kind == "presentation_batch":
            if not isinstance(value, PresentationBatch):
                self._log("worker returned an invalid presentation batch", LogLevel.WARNING)
                return False
            dirty = False
            if value.snapshot is not None:
                self.presentation.apply_snapshot(value.snapshot)
                dirty = True
            if value.delta is not None:
                try:
                    self.presentation.apply_delta(value.delta)
                except ValueError as error:
                    self._log(str(error), LogLevel.WARNING)
                else:
                    dirty = True
            if dirty:
                # Lock the old interaction surface before installing the batch's new wait. This
                # prevents the outgoing menu from being enabled and rendered once in between the
                # atomic presentation and wait halves.
                self._mark_presentation_dirty()
            self._set_active_wait(value.active_wait)
            self._presentation_commit_ready = value.render
            return dirty
        if self._handle_runtime_state_event(kind, value):
            return False
        if self._handle_export_event(kind, value):
            return False
        if self._handle_configuration_event(kind, value):
            return False
        if self._handle_fault_event(kind, value):
            return False
        self._handle_debug_or_lifecycle_event(kind, value)
        return False

    def _reset_session_state(self) -> None:
        """Drop every UI object whose contents belong to the previous runtime session."""

        self._clear_vm_ui_state()
        self.presentation = PresentationModel()
        self.runtime_phase = 0
        self.configuration_snapshot = None
        self.configuration_read_only = False
        self.project_preferences = None
        self.game_information = GameInformation()
        self._reset_client_preferences()
        self._set_debug_enabled(False)
        self._refresh_after_vm_cleanup()

    def _reset_game_state_projection(self, revision: int) -> None:
        """Drop VM-owned UI state while keeping the loaded project and preferences."""

        self._clear_vm_ui_state()
        self.presentation.retire_history(revision)
        self._refresh_after_vm_cleanup()

    def _clear_vm_ui_state(self) -> None:
        """Release UI and transfer state owned by the outgoing VM instance."""

        self._cancel_progress_loss_confirmation()
        for attribute in (
            "variable_dialog",
            "stack_dialog",
            "console_dialog",
            "fatal_dialog",
            "export_progress_dialog",
        ):
            dialog = getattr(self, attribute)
            setattr(self, attribute, None)
            if dialog is not None and dialog.is_mounted:
                dialog.dismiss(None)
        self.active_wait = None
        self._activated_wait = None
        self._pending_retired_interaction_boundary = None
        self.input_undo_token = None
        self.blocking_error = None
        self.fault_logs = ""
        self.debug_paused = False
        self.debug_location = None
        self.input_replay_exporting = False
        self.snapshot_exporting = False
        self.project_file_exporting = False
        self.diagnosis_exporting = False
        self.diagnosis_export_at_fault = False
        self.presentation_rendering = False
        self._presentation_dirty = False
        self._presentation_commit_ready = True
        self._finish_project_progress()
        self._hide_diagnosis_progress()

    def _refresh_after_vm_cleanup(self) -> None:
        self._update_prompt()
        self._refresh_menu_availability()
        self._refresh_interaction_lock()

    def _handle_runtime_state_event(self, kind: str, value: Any) -> bool:
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
        elif kind == "project_loaded":
            root, project_file = value
            self.project = Path(root)
            self.project_file = Path(project_file) if project_file else None
            self._set_status(f"项目已加载：{self.project}")
        elif kind == "game_information" and isinstance(value, GameInformation):
            self.game_information = value
        elif kind == "runtime_version":
            self.core_version = f"{value} ({CORE_REVISION})"
        else:
            return False
        return True

    def _handle_export_event(self, kind: str, value: Any) -> bool:
        if kind == "input_replay_export_finished":
            self.input_replay_exporting = False
            self._update_prompt()
            self._refresh_interaction_lock()
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
            self._hide_diagnosis_progress()
            self._update_prompt()
            self._refresh_interaction_lock()
            success, message = value
            if self.fatal_dialog is not None and self.fatal_dialog.is_mounted:
                self.fatal_dialog.finish_export(bool(success), str(message))
            elif success:
                self.notify(f"诊断信息已导出：{message}")
            else:
                self.notify(f"诊断信息导出失败：{message}", severity="error")
            self.diagnosis_export_at_fault = False
        elif kind == "diagnosis_progress" and isinstance(value, DiagnosisProgress):
            self._update_diagnosis_progress(value)
        else:
            return False
        return True

    def _handle_configuration_event(self, kind: str, value: Any) -> bool:
        if kind == "configuration":
            self.configuration_snapshot, self.configuration_read_only = value
            self._apply_client_preferences()
            if isinstance(self.screen, ProjectSettingsDialog):
                self.screen.replace_snapshot(self.configuration_snapshot)
            self._refresh_menu_availability()
        elif kind == "client_preferences_loaded":
            self.global_preferences, self.project_preferences = value
        elif kind == "client_preferences_applied":
            if isinstance(self.screen, PreferencesDialog):
                self.screen.save_finished("偏好已应用")
            self.notify("偏好已应用")
        elif kind == "client_preferences_save_failed":
            if isinstance(self.screen, PreferencesDialog):
                self.screen.save_finished(f"应用失败：{value}")
            self.notify(f"偏好应用失败：{value}", severity="error")
        elif kind == "configuration_cleared":
            self.configuration_snapshot = None
            self.configuration_read_only = False
            self.game_information = GameInformation()
            self._reset_client_preferences()
            self._refresh_menu_availability()
        elif kind == "configuration_saved":
            restart, restart_required = value
            if restart:
                if isinstance(self.screen, ProjectSettingsDialog):
                    self.screen.dismiss()
                self.notify("项目设置已保存，正在重启游戏")
            elif restart_required:
                self.notify("项目设置已保存；部分更改将在重启后生效")
            else:
                self.notify("项目设置已应用")
        elif kind == "configuration_session_applied":
            if isinstance(self.screen, ProjectSettingsDialog):
                self.screen.session_applied()
            self.notify("会话设置已应用；退出游戏后将丢失")
        elif kind == "configuration_save_failed":
            if isinstance(self.screen, ProjectSettingsDialog):
                self.screen.save_failed(str(value))
            self.notify(str(value), severity="error")
        elif kind == "open_configuration":
            self.action_project_settings()
        else:
            return False
        return True

    def _handle_fault_event(self, kind: str, value: Any) -> bool:
        if kind == "log":
            if isinstance(value, LogMessage):
                self._log(value.message, value.level, authoritative=value.authoritative)
            else:
                self._log(str(value))
        elif kind == "error":
            self._finish_project_progress()
            if isinstance(self.screen, PreferencesDialog) and self.screen.busy:
                self.screen.save_finished(f"应用失败：{value}")
            elif isinstance(self.screen, ProjectSettingsDialog) and self.screen.busy:
                self.screen.save_failed(str(value))
            self._log(str(value), LogLevel.ERROR)
            self.notify(str(value), title="RustyEra", severity="error", timeout=8)
        elif kind == "runtime_error":
            self._finish_project_progress()
            self.notify(str(value), title="RustyEra", severity="error", timeout=8)
        elif kind == "interaction_rejected":
            if self._wait_identity(value) == self._activated_wait:
                boundary = self._pending_retired_interaction_boundary
                if boundary is not None:
                    self.presentation.restore_interaction_boundary(boundary)
                self._pending_retired_interaction_boundary = None
                self._activated_wait = None
                self._queue_local_presentation_render()
                self._refresh_interaction_lock()
        elif kind == "runtime_fault":
            self._cancel_progress_loss_confirmation()
            self.input_replay_exporting = False
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
        else:
            return False
        return True

    def _handle_debug_or_lifecycle_event(self, kind: str, value: Any) -> bool:
        if kind == "debug_enabled":
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
            if self.input_replay_exporting or self.snapshot_exporting:
                self.input_replay_exporting = False
                self.snapshot_exporting = False
                self._update_prompt()
                self._refresh_interaction_lock()
            if self.diagnosis_exporting:
                self.diagnosis_exporting = False
                self._hide_diagnosis_progress()
                if self.fatal_dialog is not None and self.fatal_dialog.is_mounted:
                    self.fatal_dialog.finish_export(False, "Runtime worker 已停止")
                self.diagnosis_export_at_fault = False
                self._update_prompt()
                self._refresh_interaction_lock()
            if self.exit_pending:
                self.exit()
        else:
            return False
        return True
