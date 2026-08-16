"""Private RuntimeClient handling for rejected runtime commands."""

from __future__ import annotations

from .runtime_dependencies import (
    Any,
    COMMAND_ERROR_CODES,
    COMPILED_CACHE_RETRY_NS,
    DIAGNOSIS_EXPORT_STAGES,
    ExportStage,
    FrontendEvent,
    LogLevel,
    PendingConfigurationFinalize,
    PendingConfigurationPrepare,
    copy,
    enum_text,
    log_event,
    time,
)

_NON_NOTIFIED_INPUT_WARNINGS = {
    "input wait identity is stale",
    "input value does not match the active wait",
}


class _RuntimeRejectionMixin:
    """Classify correlated rejections before publishing a visible runtime error."""

    def _handle_command_rejection(self, value: dict[int, Any], correlation_id: int | None) -> None:
        rejection = value.get(1, "")
        non_notified_input_warning = rejection in _NON_NOTIFIED_INPUT_WARNINGS
        stale_projection = rejection in {
            "projection environment revision is not newer",
            "projection observation does not match the canonical presentation",
        }
        projection_request = correlation_id in self._projection_messages
        if projection_request:
            self._projection_messages.discard(correlation_id)
        input_request = self._input_messages.pop(correlation_id, None)
        stale_input = input_request is not None and rejection in {
            "input wait identity is stale",
            "no input is pending",
        }
        retried_input = stale_input and self._retry_stale_input(input_request)
        reload_rejection = correlation_id == getattr(self, "reload_message_id", None)
        if reload_rejection:
            self.reload_candidate = None
            self.reload_message_id = None
        if input_request is not None and not retried_input:
            self.events.put(
                FrontendEvent("interaction_rejected", copy.deepcopy(input_request.wait))
            )
        cache_export_rejection = correlation_id == self.pending_cache_export_message
        diagnosis_export_rejection = (
            correlation_id == self.pending_export_message
            and self.pending_export_kind in DIAGNOSIS_EXPORT_STAGES
        )
        snapshot_export_rejection = (
            correlation_id == self.pending_export_message
            and self.pending_export_kind == ExportStage.SNAPSHOT
        )
        project_file_export_rejection = (
            correlation_id == self.pending_export_message
            and self.pending_export_kind == ExportStage.PROJECT_FILE
        )
        pending_import = self.pending_import
        import_rejection = (
            pending_import is not None and correlation_id in pending_import.command_message_ids
        )
        pending_configuration = getattr(self, "pending_configuration", None)
        configuration_rejection = (
            isinstance(pending_configuration, PendingConfigurationPrepare)
            and correlation_id == pending_configuration.message_id
        ) or (
            isinstance(pending_configuration, PendingConfigurationFinalize)
            and correlation_id == pending_configuration.finalize_message_id
        )
        preference_rejection = correlation_id == getattr(self, "pending_client_preferences", None)
        if configuration_rejection:
            if pending_configuration.automatic:
                self.pending_start_after_configuration = None
                self.configuration_snapshot = None
                self.events.put(
                    FrontendEvent(
                        "error",
                        f"确认 reraconfig.toml 迁移失败：{rejection}",
                    )
                )
            self.pending_configuration = None
        if preference_rejection:
            self.pending_client_preferences = None
            was_save = self.pending_client_preferences_save
            self.pending_client_preferences_save = False
            cache_hit = self.pending_start_after_preferences
            self.pending_start_after_preferences = None
            self.events.put(log_event(f"客户端偏好未应用：{rejection}", LogLevel.WARNING))
            if cache_hit is not None:
                self._continue_project_start(cache_hit)
            elif was_save:
                self.events.put(FrontendEvent("client_preferences_save_failed", str(rejection)))
        if import_rejection:
            self._fail_pending_import(f"状态导入命令被拒绝：{rejection}")
        elif cache_export_rejection:
            self.pending_export = None
            self.pending_cache_export_message = None
            if value.get(0) == 0:  # InvalidState: retry the caller-pumped preparation.
                self.pending_cache_after = None
                self.pending_export_kind = None
                self.cache_refresh_pending = True
                self.cache_ready = False
                self.cache_refresh_after_ns = time.monotonic_ns() + COMPILED_CACHE_RETRY_NS
            else:
                self._finish_cache_export(False)
        elif diagnosis_export_rejection:
            stage = self.pending_export_kind
            self.pending_export = None
            self.pending_export_message = None
            if (
                stage == ExportStage.DIAGNOSIS_PROJECT
                and value.get(0) == 0
                and self.pending_diagnosis is not None
            ):
                self.pending_export_kind = None
                self.pending_diagnosis.stage = "project_wait"
                self.pending_diagnosis.retry_after_ns = (
                    time.monotonic_ns() + COMPILED_CACHE_RETRY_NS
                )
            else:
                self._finish_diagnosis_export(False, f"命令被拒绝：{rejection}")
        elif snapshot_export_rejection:
            self.pending_export = None
            self.pending_export_kind = None
            self.pending_export_message = None
            self.events.put(FrontendEvent("snapshot_export_finished", False))
        elif project_file_export_rejection:
            self.pending_export = None
            self.pending_export_kind = None
            self.pending_export_message = None
            if value.get(0) == 0:
                if self.full_project_export is not None:
                    self.full_project_export.retry_after_ns = (
                        time.monotonic_ns() + COMPILED_CACHE_RETRY_NS
                    )
            else:
                self._finish_project_file_export(False)
        elif configuration_rejection:
            self.events.put(
                FrontendEvent("configuration_save_failed", f"保存设置被拒绝：{rejection}")
            )
        elif preference_rejection:
            pass
        # A presentation may advance after the frontend rendered an observation but
        # before the caller-pumped runtime handles it. This is a benign stale sample;
        # a later rendered revision will submit a replacement observation.
        publish_unhandled_rejection = (
            not (
                cache_export_rejection
                or diagnosis_export_rejection
                or project_file_export_rejection
                or import_rejection
                or configuration_rejection
                or preference_rejection
                or reload_rejection
            )
            and not (projection_request and value.get(0) == 2 and stale_projection)
            and not retried_input
        )
        if non_notified_input_warning or publish_unhandled_rejection:
            code = enum_text(value.get(0), COMMAND_ERROR_CODES, "CommandErrorCode")
            message = f"命令被拒绝 [{code}]：{rejection}"
            if non_notified_input_warning:
                self.events.put(log_event(message, LogLevel.WARNING))
            else:
                self.events.put(FrontendEvent("runtime_error", message))
