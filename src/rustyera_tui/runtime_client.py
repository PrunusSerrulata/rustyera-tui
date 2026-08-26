"""Runtime client orchestration over private responsibility mixins."""

from __future__ import annotations

from .runtime_dependencies import (
    Any,
    CORE_STARTUP_PHASES,
    ConfigurationSnapshot,
    DiagnosisExport,
    DiagnosisProgress,
    DiagnosisProgressStage,
    ExportStage,
    FrontendEvent,
    FullProjectExport,
    LogLevel,
    Path,
    PendingConfigurationFinalize,
    PendingConfigurationPrepare,
    PendingGameInput,
    PendingStateImport,
    PresentationBatch,
    PresentationEventAccumulator,
    ProjectBundle,
    RuntimeAbi,
    RuntimeFailure,
    ServicePresentationModel,
    StorageBackend,
    copy,
    emit_startup_milestone,
    log_event,
    queue,
    runtime_log_level,
    time,
    variant,
    _PendingExport,
)

from .runtime_debug import _RuntimeDebugMixin
from .runtime_interaction import _RuntimeInteractionMixin
from .runtime_project import _RuntimeProjectMixin
from .runtime_rejections import _RuntimeRejectionMixin
from .runtime_transport import _RuntimeTransportMixin
from .runtime_transfer import _RuntimeTransferMixin
from .client_preferences import (
    LoadedPreferences,
    global_preferences_path,
    load_preferences,
)


class RuntimeClient(
    _RuntimeTransportMixin,
    _RuntimeProjectMixin,
    _RuntimeTransferMixin,
    _RuntimeRejectionMixin,
    _RuntimeDebugMixin,
    _RuntimeInteractionMixin,
):
    """Translate frontend intent to the public runtime and debug wire protocols."""

    def __init__(
        self,
        abi: RuntimeAbi,
        events: queue.Queue[FrontendEvent],
        *,
        new_game_seed: int | None = None,
        metrics_threshold_ms: float | None = None,
    ) -> None:
        self.abi = abi
        self.events = events
        self.new_game_seed = new_game_seed
        self.metrics_threshold_ms = metrics_threshold_ms
        self.runtime_sequence = 0
        self.debug_sequence = 0
        self.next_message_id = 1
        self.session: dict[int, int] | None = None
        self.epoch: int | None = None
        self.phase = 0
        self.expected_runtime_output = 0
        self.expected_debug_output = 0
        self.bundle: ProjectBundle | None = None
        self.pending_bundle: ProjectBundle | None = None
        self.reload_candidate: ProjectBundle | None = None
        self.reload_message_id: int | None = None
        self.storage: StorageBackend | None = None
        self.presentation = ServicePresentationModel()
        self.active_wait: dict[int, Any] | None = None
        self.pending_restore: tuple[Path, bytes | None, str] | None = None
        self.pending_export: _PendingExport | None = None
        self.pending_diagnosis: DiagnosisExport | None = None
        self.pending_import: PendingStateImport | None = None
        self.pending_cache_after: str | None = None
        self.cache_refresh_pending = False
        self.cache_ready = False
        self.cache_refresh_after_ns = 0
        self.cache_preparation_started = False
        self.cache_refresh_after = "background"
        self.allow_compiled_cache_load = True
        self.pending_project_file_bytes: bytes | None = None
        self.full_project_export: FullProjectExport | None = None
        self.pending_configuration: (
            PendingConfigurationPrepare | PendingConfigurationFinalize | None
        ) = None
        self.pending_start_after_configuration: bool | None = None
        self.configuration_snapshot: ConfigurationSnapshot | None = None
        self.configuration_profile_supported = False
        self.global_preferences = load_preferences(global_preferences_path())
        self.project_preferences: LoadedPreferences | None = None
        self.events.put(
            FrontendEvent(
                "client_preferences_loaded",
                (self.global_preferences, self.project_preferences),
            )
        )
        self.pending_client_preferences: int | None = None
        self.pending_client_preferences_save = False
        self.pending_start_after_preferences: bool | None = None
        self.debug_requested = False
        self.debug_grant: dict[int, Any] | None = None
        self.stop_token: dict[int, Any] | None = None
        self.selected_fiber: int | None = None
        self.pending_debug_actions: list[tuple[str, Any]] = []
        self.debug_pending_by_message: dict[int, str] = {}
        self.debug_pending_cost_by_message: dict[int, int] = {}
        self.deferred_debug_refresh: dict[str, tuple[list[Any], int]] = {}
        self.deferred_debug_console: list[tuple[list[Any], int]] = []
        self.debug_backpressure_warnings: set[str] = set()
        self.single_step_enabled = False
        self.debug_step_in_flight = False
        self.debug_disable_pending = False
        self.transient_pause_owner: str | None = None
        self.transient_close_pending: str | None = None
        self.last_time_advance_ns = 0
        self.shutting_down = False
        self.startup_attempt = 0
        self.startup_scenario = "cold"
        self.startup_active = False
        self.startup_start_submitted = False
        self.startup_first_phase_reported = False
        self.startup_host_durations: dict[str, float | bool | int] = {}
        self.source_index_misses: tuple[str, ...] = ()
        self.startup_core_durations: dict[str, float] = {}
        self._startup_core_phase_started: dict[int, int] = {}
        self._pending_presentation = PresentationEventAccumulator()
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        self._projection_messages: set[int] = set()
        self._input_messages: dict[int, PendingGameInput] = {}
        self._session_reset_active = False
        self._session_destroy_pending = False
        self._replacement_session_prepared = False
        self._send_hello()

    def _project_scan_progress(self, completed: int, total: int) -> None:
        if self.pending_diagnosis is not None and self.pending_diagnosis.stage == "project":
            self._report_diagnosis_progress("project_scanning", completed, total)
        else:
            self.events.put(FrontendEvent("project_progress", (0, completed, total)))

    def _report_diagnosis_progress(
        self, stage: DiagnosisProgressStage, completed: int = 0, total: int = 0
    ) -> None:
        self.events.put(
            FrontendEvent(
                "diagnosis_progress",
                DiagnosisProgress(stage, max(0, completed), max(0, total)),
            )
        )

    def report_runtime_project_progress(self, stage: int, completed: int, total: int) -> None:
        phase_name = CORE_STARTUP_PHASES.get(stage)
        now = time.monotonic_ns()
        if phase_name is not None and completed == 0:
            self._startup_core_phase_started.setdefault(stage, now)
        if phase_name is not None and total > 0 and completed >= total:
            started = self._startup_core_phase_started.pop(stage, None)
            if started is not None:
                duration_ms = (now - started) / 1e6
                self.startup_core_durations[phase_name] = duration_ms
                emit_startup_milestone(
                    "core_phase",
                    attempt_id=self.startup_attempt,
                    stage=stage,
                    phase=phase_name,
                    duration_ms=duration_ms,
                )
        if (
            self.pending_diagnosis is not None
            and self.pending_export is not None
            and self.pending_export.stage == ExportStage.DIAGNOSIS_PROJECT
        ):
            self._report_diagnosis_progress(
                "project_packaging" if stage == 9 else "project_preparing",
                completed,
                total,
            )
        else:
            self.events.put(FrontendEvent("project_progress", (stage, completed, total)))

    def _reset_wire_state(self) -> None:
        if self.pending_import is not None:
            self._clear_pending_import()
        self.runtime_sequence = 0
        self.debug_sequence = 0
        self.next_message_id = 1
        self.session = None
        self.epoch = None
        self.phase = 0
        self.expected_runtime_output = 0
        self.expected_debug_output = 0
        self.presentation = ServicePresentationModel()
        self.active_wait = None
        self.debug_requested = False
        self.debug_grant = None
        self.stop_token = None
        self.selected_fiber = None
        self.pending_debug_actions.clear()
        self.debug_pending_by_message.clear()
        self.debug_pending_cost_by_message.clear()
        self.deferred_debug_refresh.clear()
        self.deferred_debug_console.clear()
        self.debug_backpressure_warnings.clear()
        self.single_step_enabled = False
        self.debug_step_in_flight = False
        self.debug_disable_pending = False
        self.transient_pause_owner = None
        self.transient_close_pending = None
        self.shutting_down = False
        self._pending_presentation.clear()
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        self._projection_messages.clear()
        self._input_messages.clear()
        self.reload_candidate = None
        self.reload_message_id = None
        if self.pending_export is not None:
            self.pending_export.cancel()
            self.pending_export = None
        if self.pending_diagnosis is not None:
            self.pending_diagnosis.cleanup()
            self.pending_diagnosis = None
        self.pending_cache_after = None
        self.cache_refresh_pending = False
        self.cache_ready = False
        self.cache_refresh_after_ns = 0
        self.cache_preparation_started = False
        self.cache_refresh_after = "background"
        if self.full_project_export is not None:
            self.full_project_export.stream.cancel()
            self.full_project_export = None
        self.pending_configuration = None
        self.pending_start_after_configuration = None
        self.pending_client_preferences = None
        self.pending_client_preferences_save = False
        self.pending_start_after_preferences = None
        self.project_preferences = None
        self.configuration_snapshot = None
        self.configuration_profile_supported = False
        self.bundle = None
        self.pending_bundle = None
        self.storage = None
        self.pending_restore = None
        self.pending_project_file_bytes = None

    def begin_game_state_transition(
        self, message_id: int, *, shutting_down: bool = False
    ) -> None:
        """Retire VM-owned frontend state after a replacement command is submitted."""

        revision = self.presentation.begin_replacement(message_id)
        self._pending_presentation.clear()
        self.active_wait = None
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        self._projection_messages.clear()
        self._input_messages.clear()
        self.stop_token = None
        self.selected_fiber = None
        self.pending_debug_actions.clear()
        self.debug_pending_by_message.clear()
        self.debug_pending_cost_by_message.clear()
        self.deferred_debug_refresh.clear()
        self.deferred_debug_console.clear()
        self.debug_backpressure_warnings.clear()
        self.debug_step_in_flight = False
        self.transient_pause_owner = None
        self.transient_close_pending = None
        self._retire_transfers_for_game_transition(
            reschedule_cache=not shutting_down
        )
        self.events.put(FrontendEvent("game_state_reset", revision))

    def begin_session_reset(self) -> None:
        """Release the active session and all session-owned frontend projections once."""

        if not self._session_reset_active:
            self._session_reset_active = True
            self._session_destroy_pending = True
            self.events.put(FrontendEvent("session_reset"))
        if not self._session_destroy_pending:
            return
        self._replacement_session_prepared = False
        try:
            self.abi.destroy_session()
        except BaseException as error:
            # Python-side state must not keep the old game alive when scanning or session
            # destruction fails. A later restart attempt may create a fresh session.
            self._reset_wire_state_preserving(error)
            raise
        self._session_destroy_pending = False
        self._reset_wire_state()

    @property
    def can_pump(self) -> bool:
        """Whether the ABI currently owns a session that may be driven."""

        return not self._session_reset_active

    @staticmethod
    def _annotate_cleanup_failure(
        error: BaseException, operation: str, cleanup_error: BaseException
    ) -> None:
        add_note = getattr(error, "add_note", None)
        if add_note is not None:
            add_note(f"additionally failed to {operation}: {cleanup_error}")

    def _reset_wire_state_preserving(self, error: BaseException) -> None:
        try:
            self._reset_wire_state()
        except BaseException as cleanup_error:
            self._annotate_cleanup_failure(error, "clear frontend session state", cleanup_error)

    def abort_session_replacement(self, error: BaseException) -> None:
        """Best-effort cleanup which preserves the replacement operation's exception."""

        try:
            self.abi.destroy_session()
        except BaseException as cleanup_error:
            self._session_destroy_pending = True
            self._annotate_cleanup_failure(error, "destroy replacement session", cleanup_error)
        else:
            self._session_destroy_pending = False
        finally:
            self._replacement_session_prepared = False
            self._session_reset_active = True
            self._reset_wire_state_preserving(error)

    def prepare_replacement_session(self) -> None:
        """Create one empty session for manifest decoding and the subsequent hello."""

        if self._replacement_session_prepared:
            return
        self.begin_session_reset()
        try:
            self.abi.create_session()
        except BaseException as error:
            self.abort_session_replacement(error)
            raise
        self._replacement_session_prepared = True

    def recreate(
        self,
        bundle: ProjectBundle,
        restore: tuple[Path, bytes | None, str] | None = None,
        *,
        allow_compiled_cache: bool = True,
        project_file_bytes: bytes | None = None,
    ) -> None:
        if not self.startup_active:
            self.begin_startup_attempt(project_file=bundle.project_file is not None)
        self.events.put(FrontendEvent("configuration_cleared"))
        self.events.put(FrontendEvent("status", "正在创建新的 Runtime session…"))
        if not self._replacement_session_prepared:
            # prepare_replacement_session owns cleanup for create failures. Keeping it outside
            # this try avoids destroying the same partial handle twice when creation fails.
            self.prepare_replacement_session()
        try:
            self.pending_bundle = bundle
            self.pending_restore = restore
            self.allow_compiled_cache_load = allow_compiled_cache
            self.pending_project_file_bytes = project_file_bytes
            self.storage = self._storage_for_bundle(bundle)
            self._send_hello()
        except BaseException as error:
            self.abort_session_replacement(error)
            raise
        self._replacement_session_prepared = False
        self._session_destroy_pending = False
        self._session_reset_active = False

    def begin_startup_attempt(self, *, project_file: bool) -> None:
        self.startup_attempt += 1
        self.startup_scenario = "project_file" if project_file else "cold"
        self.startup_active = True
        self.startup_start_submitted = False
        self.startup_first_phase_reported = False
        self.startup_host_durations = {}
        self.source_index_misses = ()
        self.startup_core_durations = {}
        self._startup_core_phase_started = {}
        emit_startup_milestone(
            "attempt_started",
            attempt_id=self.startup_attempt,
            scenario=self.startup_scenario,
        )

    def record_host_metrics(self, metrics: dict[str, float | bool | int]) -> None:
        if not hasattr(self, "startup_host_durations"):
            self.startup_host_durations = {}
        self.startup_host_durations.update(metrics)
        emit_startup_milestone(
            "host_metrics",
            attempt_id=getattr(self, "startup_attempt", 0),
            **metrics,
        )

    def record_host_duration(self, field: str, started_ns: int) -> None:
        duration_ms = (time.monotonic_ns() - started_ns) / 1e6
        self.record_host_metrics({field: duration_ms})

    def fail_startup(self, error: object) -> None:
        if not self.startup_active:
            return
        emit_startup_milestone(
            "failed",
            attempt_id=self.startup_attempt,
            scenario=self.startup_scenario,
            error=str(error),
        )
        self.startup_active = False
        self._startup_core_phase_started.clear()

    def _new_game_start(self) -> dict[int, Any]:
        """Build the normal start request, optionally using a deterministic test seed."""

        return {0: variant(0, self.new_game_seed)}

    def _flush_presentation_events(self) -> None:
        render = self._presentation_boundary_dirty or self.active_wait is not None
        if not render:
            # Textual never commits a running batch. Keep presentation work on the worker side
            # until a visible boundary, while still retiring a cleared wait promptly so stale
            # input cannot remain locally actionable.
            if self._wait_event_dirty:
                self.events.put(
                    FrontendEvent(
                        "presentation_batch",
                        PresentationBatch(None, None, copy.deepcopy(self.active_wait), False),
                    )
                )
            return
        snapshot, delta = self._pending_presentation.take()
        if (
            snapshot is None
            and delta is None
            and not (self._wait_event_dirty or self._presentation_boundary_dirty)
        ):
            return
        # A single queue item prevents Textual from observing presentation and wait halves
        # from different runtime pumps.
        self.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(
                    snapshot,
                    delta,
                    copy.deepcopy(self.active_wait),
                    render,
                ),
            )
        )

    def _handle_runtime(self, tag: int, value: Any, correlation_id: int | None) -> None:
        if tag == 1:  # ServerHello
            self.session = value[1]
            self.epoch = value[4]
            self.configuration_profile_supported = value.get(7) == 1
            self.events.put(FrontendEvent("runtime_version", value.get(8, "unknown")))
            if self.pending_bundle is not None:
                if self.pending_project_file_bytes is not None:
                    project_file = self.pending_project_file_bytes
                    self.pending_project_file_bytes = None
                    self.events.put(FrontendEvent("status", "正在载入项目文件…"))
                    self._stage_project_cache(project_file, "project_file")
                    return
                self._stage_persistent_cache_or_source()
            return
        if tag == 2:
            self.fail_startup(f"protocol version rejected: {value.get(1, '')}")
            self.events.put(FrontendEvent("runtime_error", f"协议版本被拒绝：{value.get(1, '')}"))
        elif tag == 11:  # ProjectLoadReport
            self._handle_project_report(value)
        elif tag == 25:  # ConfigurationUpdatePrepared
            self._handle_configuration_prepared(value, correlation_id)
        elif tag == 27:  # ConfigurationUpdateCommitted
            self._handle_configuration_committed(value, correlation_id)
        elif tag == 29:  # ClientPreferencesApplied
            self._handle_client_preferences_applied(value, correlation_id)
        elif tag == 21:
            self.phase = value[0]
            self.epoch = value[2]
            if (
                self.startup_active
                and self.startup_start_submitted
                and not self.startup_first_phase_reported
                and self.phase in {4, 5, 6}
            ):
                self.startup_first_phase_reported = True
                self.startup_active = False
                emit_startup_milestone(
                    "first_game_phase",
                    attempt_id=self.startup_attempt,
                    scenario=self.startup_scenario,
                    phase=self.phase,
                )
            elif self.startup_active and self.phase in {10, 11}:
                self.fail_startup(f"runtime entered terminal phase {self.phase}")
            if self.phase in {7, 9, 10, 11}:
                self._presentation_boundary_dirty = True
            self.events.put(FrontendEvent("phase", self.phase))
        elif tag == 22:
            self._presentation_boundary_dirty = True
            reason = "重启" if value[0] == 1 else "退出"
            self.events.put(FrontendEvent("exit_requested", reason))
        elif tag == 32:
            self._handle_wait_change(value)
        elif tag == 36:
            self.events.put(FrontendEvent("text_box", value.get(1, "")))
        elif tag == 38:
            self.events.put(FrontendEvent("input_undo", value))
        elif tag == 40:
            self.presentation.apply_snapshot(value)
            wait_changed = self._set_active_wait(self.presentation.input_wait)
            # Decoded envelopes are immutable after dispatch. The accumulator reduces later
            # deltas incrementally until a visible boundary crosses to Textual.
            self._pending_presentation.replace_snapshot(value)
            self._wait_event_dirty = self._wait_event_dirty or wait_changed
        elif tag == 41:
            try:
                projected_delta = self.presentation.apply_delta(value)
            except ValueError as error:
                self.events.put(log_event(str(error), LogLevel.WARNING))
                self.send_runtime(94, {0: self.expected_runtime_output - 1})
            else:
                wait_changed = self._set_active_wait(self.presentation.input_wait)
                self._pending_presentation.add_delta(projected_delta)
                self._wait_event_dirty = self._wait_event_dirty or wait_changed
        elif tag == 42:  # Effects are intentionally unsupported but must be acknowledged.
            self._acknowledge_effects(value)
        elif tag == 50:
            self._handle_storage(value, correlation_id)
        elif tag == 52:
            self._handle_service(value, correlation_id)
        elif tag == 61:
            self._handle_export_ready(value, correlation_id)
        elif tag == 63:
            self._handle_import_accepted(value, correlation_id)
        elif tag == 66:
            self._handle_import_ready(value, correlation_id)
        elif tag == 68:
            self._handle_export_chunk(value)
        elif tag == 91:
            self._presentation_boundary_dirty = True
            self.events.put(FrontendEvent("shutdown_ready", value))
        elif tag == 92:
            if self.pending_import is not None:
                self._clear_pending_import()
            self._presentation_boundary_dirty = True
            origin = value.get(2) or {}
            source = origin.get(4) or {}
            failure = RuntimeFailure(
                code=value.get(0, -1),
                message=value.get(1, ""),
                command=origin.get(0),
                function=origin.get(1),
                source_path=source.get(0),
                source_line=source.get(3),
            )
            self.events.put(FrontendEvent("runtime_fault", failure))
        elif tag == 95:
            self._handle_command_rejection(value, correlation_id)
        elif tag == 96:
            self.epoch = value[0]
            self.phase = value[1]
            self.presentation.apply_snapshot(value[3])
            wait_changed = self._set_active_wait(self.presentation.input_wait)
            self._pending_presentation.replace_snapshot(value[3])
            self._wait_event_dirty = self._wait_event_dirty or wait_changed
            self._publish_configuration(value.get(8))
        elif tag == 97:
            source = value.get(3)
            location = f" ({source.get(0)}:{source.get(3, '?')})" if source else ""
            self.events.put(
                log_event(
                    f"[{value.get(0)}]: {value.get(2)}{location}",
                    runtime_log_level(value.get(1)),
                    authoritative=True,
                )
            )
            if value.get(0) == "runtime.compiled_cache_ready":
                self.cache_ready = True
                self.cache_refresh_after_ns = 0
            elif value.get(0) == "runtime.snapshot_restored_from_debug":
                self.events.put(
                    FrontendEvent(
                        "snapshot_restore_warning",
                        "该快照由调试模式导出；已确认其状态可恢复，但内容可能包含调试影响。",
                    )
                )
            elif value.get(0) == "runtime.snapshot_restored_from_diagnosis":
                self.events.put(
                    FrontendEvent(
                        "snapshot_restore_warning",
                        "该快照来自诊断信息；已确认其状态可恢复，请仅用于问题排查。",
                    )
                )
        elif tag == 98:
            self.events.put(
                log_event(
                    str(value.get(1, "")),
                    runtime_log_level(value.get(0)),
                    authoritative=True,
                )
            )

    def projection(
        self,
        width: int,
        height: int,
        environment_revision: int,
        presentation_revision: int,
    ) -> None:
        if self.session is None or presentation_revision != self.presentation.revision:
            return
        message_id = self.send_runtime(
            35,
            {
                0: environment_revision,
                1: presentation_revision,
                2: {0: width, 1: height},
                3: environment_revision,
                4: max(1, width),
                5: "",
                6: {0: 1, 1: 1000, 2: 1, 3: 1000, 4: 0, 5: 0},
            },
        )
        if len(self._projection_messages) >= 256:
            self._projection_messages.clear()
        self._projection_messages.add(message_id)

    def shutdown(self) -> None:
        if not self.shutting_down and self.session is not None:
            if self.pending_import is not None:
                self._cancel_pending_import()
            self.shutting_down = True
            message_id = self.send_runtime(90, {0: True})
            self.begin_game_state_transition(message_id, shutting_down=True)
