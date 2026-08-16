"""Private RuntimeClient responsibilities extracted from the compatibility facade."""

from __future__ import annotations

from .client_preferences import (
    PreferenceValues,
    load_preferences,
    project_preferences_path,
    save_preferences,
)
from .runtime_dependencies import (
    APPLICATION_HOT,
    Any,
    ConfigurationChange,
    ConfigurationSnapshot,
    FrontendEvent,
    GameInformation,
    LogLevel,
    PendingConfigurationFinalize,
    PendingConfigurationPrepare,
    PreparedConfiguration,
    PreparedConfigurationAbort,
    PreparedConfigurationCommit,
    ProjectBundle,
    blake3,
    emit_startup_milestone,
    format_project_diagnostic,
    log_event,
    runtime_log_level,
    time,
)
from .runtime_project_reload import _RuntimeProjectReloadMixin


class _RuntimeProjectMixin(_RuntimeProjectReloadMixin):
    def _preference_changes(self, values: PreferenceValues) -> list[dict[int, str]]:
        snapshot = self.configuration_snapshot
        if snapshot is None:
            return []
        eligible = {entry.code for entry in snapshot.tui_preference_entries}
        return [{0: code, 1: value} for code, value in values.settings.items() if code in eligible]

    def _submit_client_preferences(self) -> int:
        if self.configuration_snapshot is None:
            raise RuntimeError("没有已载入的项目配置")
        project_values = (
            self.project_preferences.values
            if self.project_preferences is not None
            else PreferenceValues({})
        )
        return self.send_runtime(
            28,
            {
                0: self.configuration_snapshot.project_revision,
                1: self._preference_changes(self.global_preferences.values),
                2: self._preference_changes(project_values),
            },
        )

    def _handle_project_report(self, report: dict[int, Any]) -> None:
        from . import runtime as runtime_facade

        cache_hit = False
        diagnostic_bundle = next(
            (
                bundle
                for bundle in (self.reload_candidate, self.pending_bundle, self.bundle)
                if bundle is not None and bundle.revision == report.get(0)
            ),
            None,
        )
        for diagnostic in report.get(2, []):
            source = diagnostic.get(3)
            source_text = None
            if isinstance(source, dict) and diagnostic_bundle is not None:
                relative_path = str(source.get(0, ""))
                item = diagnostic_bundle.files.get(relative_path)
                if item is None:
                    item = next(
                        (
                            candidate
                            for path, candidate in diagnostic_bundle.files.items()
                            if path.casefold() == relative_path.casefold()
                        ),
                        None,
                    )
                if (
                    item is not None
                    and item.payload is not None
                    and item.payload[0] == 0
                    and item.payload[1]
                    and isinstance(item.payload[1][0], str)
                ):
                    source_text = item.payload[1][0]
            self.events.put(
                log_event(
                    format_project_diagnostic(diagnostic, source_text),
                    runtime_log_level(diagnostic.get(1)),
                    authoritative=True,
                )
            )
            cache_hit = cache_hit or diagnostic.get(0) == "runtime.compiled_cache_hit"
        if report.get(3, False):
            if self.pending_bundle is None:
                self.fail_startup("runtime requested source without a pending project")
                self.events.put(FrontendEvent("error", "Runtime 请求源码，但没有待载入项目。"))
                return
            self.events.put(FrontendEvent("status", "项目缓存未命中，正在读取项目源码…"))
            materialize_started = time.monotonic_ns()
            self.pending_bundle = self.pending_bundle.materialize(self._project_scan_progress)
            self.record_host_duration("source_materialize_ms", materialize_started)
            self._submit_project(None)
            return
        if not report[1]:
            self.fail_startup("project load failed")
            self.reload_candidate = None
            self.reload_message_id = None
            self.events.put(FrontendEvent("runtime_error", "项目加载或热重载失败，请查看日志。"))
            return
        if self.reload_candidate is not None and report[0] == self.reload_candidate.revision:
            self.bundle = self.reload_candidate
            self.reload_candidate = None
            self.reload_message_id = None
            self.storage = self._storage_for_bundle(self.bundle)
            self.cache_refresh_pending = True
            self.cache_preparation_started = False
            self.cache_refresh_after = "reload"
            self.cache_refresh_after_ns = (
                time.monotonic_ns() + runtime_facade.COMPILED_CACHE_PERSIST_DELAY_NS
            )
            self.events.put(FrontendEvent("status", "脚本热重载完成。"))
            self.events.put(
                FrontendEvent("game_information", GameInformation.from_wire(report.get(5)))
            )
            self._publish_configuration(report.get(4))
            return
        if self.pending_bundle is not None:
            if cache_hit:
                self.pending_bundle.reload_baseline_pending = True
            else:
                self.pending_bundle.reload_baseline_pending = False
            self.bundle = self.pending_bundle
            self.pending_bundle = None
            self.storage = self._storage_for_bundle(self.bundle)
        self.events.put(
            FrontendEvent(
                "project_loaded",
                (self.bundle.root, self.bundle.project_file) if self.bundle else None,
            )
        )
        self.events.put(FrontendEvent("game_information", GameInformation.from_wire(report.get(5))))
        self._publish_configuration(report.get(4))
        if self.startup_active:
            self.startup_scenario = (
                "project_file"
                if self.bundle is not None and self.bundle.project_file is not None
                else "warm"
                if cache_hit
                else "cold"
            )
            emit_startup_milestone(
                "validation_complete",
                attempt_id=self.startup_attempt,
                scenario=self.startup_scenario,
                cache_hit=cache_hit,
            )
        if isinstance(self.pending_configuration, PendingConfigurationPrepare) and (
            self.pending_configuration.automatic
        ):
            self.pending_start_after_configuration = cache_hit
            return
        self._begin_client_preferences(cache_hit)

    def _begin_client_preferences(self, cache_hit: bool | None = None) -> None:
        if self.bundle is None or self.configuration_snapshot is None:
            if cache_hit is not None:
                self._continue_project_start(cache_hit)
            return
        self.project_preferences = load_preferences(project_preferences_path(self.bundle))
        for loaded in (self.global_preferences, self.project_preferences):
            if loaded.error:
                self.events.put(log_event(loaded.error, LogLevel.WARNING))
        message_id = self._submit_client_preferences()
        self.pending_client_preferences = message_id
        self.pending_client_preferences_save = False
        self.pending_start_after_preferences = cache_hit
        self.events.put(
            FrontendEvent(
                "client_preferences_loaded",
                (self.global_preferences, self.project_preferences),
            )
        )

    def save_client_preferences(self, scope: str, values: PreferenceValues) -> None:
        if self.pending_client_preferences is not None:
            self.events.put(FrontendEvent("client_preferences_save_failed", "偏好操作仍在进行"))
            return
        try:
            if scope == "global":
                self.global_preferences = save_preferences(self.global_preferences, values)
            elif scope == "project":
                if self.project_preferences is None:
                    raise RuntimeError("当前没有可保存项目偏好的项目")
                self.project_preferences = save_preferences(self.project_preferences, values)
            else:
                raise ValueError(f"未知偏好范围：{scope}")
        except (OSError, ValueError, RuntimeError) as error:
            self.events.put(FrontendEvent("client_preferences_save_failed", str(error)))
            return
        self.events.put(
            FrontendEvent(
                "client_preferences_loaded",
                (self.global_preferences, self.project_preferences),
            )
        )
        if self.configuration_snapshot is None:
            self.events.put(FrontendEvent("client_preferences_applied"))
            return
        self.pending_client_preferences = self._submit_client_preferences()
        self.pending_client_preferences_save = True
        self.pending_start_after_preferences = None

    def _handle_client_preferences_applied(
        self, value: dict[int, Any], correlation_id: int | None
    ) -> None:
        if correlation_id != self.pending_client_preferences:
            self.events.put(log_event("忽略了非预期的客户端偏好响应", LogLevel.WARNING))
            return
        self.pending_client_preferences = None
        try:
            snapshot = ConfigurationSnapshot.from_wire(value.get(0))
        except ValueError as error:
            raise RuntimeError(f"Runtime 返回了无效的客户端偏好画像：{error}") from error
        self.configuration_snapshot = snapshot
        self.events.put(FrontendEvent("configuration", (snapshot, False)))
        cache_hit = self.pending_start_after_preferences
        self.pending_start_after_preferences = None
        was_save = self.pending_client_preferences_save
        self.pending_client_preferences_save = False
        if cache_hit is not None:
            self._continue_project_start(cache_hit)
        elif was_save:
            self.events.put(FrontendEvent("client_preferences_applied"))

    def _continue_project_start(self, cache_hit: bool) -> None:
        from . import runtime as runtime_facade

        if self.pending_restore is not None:
            _path, payload, purpose = self.pending_restore
            kind = 0 if purpose == "traditional_save" else 1
            self._begin_import(payload, kind, purpose)
            return
        if cache_hit:
            self.cache_refresh_pending = False
            self.cache_ready = False
            self.cache_preparation_started = False
            self.events.put(FrontendEvent("status", "项目缓存命中，正在进入标题画面…"))
            self._submit_start(self._new_game_start())
        else:
            self.cache_refresh_pending = True
            self.cache_preparation_started = False
            self.cache_refresh_after = "background"
            self.cache_refresh_after_ns = (
                time.monotonic_ns() + runtime_facade.COMPILED_CACHE_PERSIST_DELAY_NS
            )
            self.events.put(FrontendEvent("status", "项目编译完成，正在进入标题画面…"))
            self._submit_start(self._new_game_start())

    def _submit_start(self, value: dict[int, Any]) -> None:
        self.send_runtime(20, value)
        if not getattr(self, "startup_active", False):
            return
        self.startup_start_submitted = True
        emit_startup_milestone(
            "start_submitted",
            attempt_id=self.startup_attempt,
            scenario=self.startup_scenario,
        )

    def _publish_configuration(
        self, value: Any, *, confirm_generated: bool = True
    ) -> ConfigurationSnapshot | None:
        if value is None:
            return None
        try:
            snapshot = ConfigurationSnapshot.from_wire(value)
        except ValueError as error:
            self.events.put(FrontendEvent("error", f"Runtime 返回了无效的项目配置：{error}"))
            return None
        self.configuration_snapshot = snapshot
        read_only = (
            self.bundle is not None
            and self.bundle.project_file is not None
            and not self.abi.supports_project_configuration_updates
        )
        if snapshot.generated_source is not None and self.bundle is not None and not read_only:
            try:
                self.bundle = self._write_configuration(
                    self.bundle, snapshot.source_digest, snapshot.generated_source
                )
                if confirm_generated and self.pending_configuration is None:
                    self._begin_configuration_update(snapshot, [], False, False, True)
            except (OSError, RuntimeError, UnicodeError, ValueError) as error:
                self.configuration_snapshot = None
                self.events.put(FrontendEvent("error", f"迁移 reraconfig.toml 失败：{error}"))
                return None
        self.events.put(FrontendEvent("configuration", (snapshot, read_only)))
        return snapshot

    def prepare_configuration_update(
        self, changes: list[ConfigurationChange], restart: bool = False
    ) -> None:
        if self.bundle is None:
            raise RuntimeError("没有已载入的项目")
        if self.pending_configuration is not None:
            raise RuntimeError("偏好选项正在保存")
        snapshot = self.configuration_snapshot
        if snapshot is None:
            raise RuntimeError("Runtime 尚未提供项目配置")
        if not self.configuration_profile_supported:
            raise RuntimeError("当前 Runtime 不支持 TUI 项目设置画像")
        session_only = (
            self.bundle.project_file is not None
            and not self.abi.supports_project_configuration_updates
        )
        if session_only:
            entries = {entry.code.casefold(): entry for entry in snapshot.tui_entries}
            if restart or any(
                (entry := entries.get(change.code.casefold())) is None
                or entry.fixed
                or entry.application != APPLICATION_HOT
                for change in changes
            ):
                raise PermissionError("项目文件仅支持当前会话内即时生效的设置")
        self._begin_configuration_update(snapshot, changes, restart, session_only, False)

    def _begin_configuration_update(
        self,
        snapshot: ConfigurationSnapshot,
        changes: list[ConfigurationChange],
        restart: bool,
        session_only: bool,
        automatic: bool,
    ) -> None:
        message_id = self.send_runtime(24, snapshot.prepare_wire(changes))
        self.pending_configuration = PendingConfigurationPrepare(
            message_id,
            snapshot.project_revision,
            snapshot.source_digest,
            restart,
            session_only,
            automatic,
        )

    def _handle_configuration_prepared(
        self, prepared: dict[int, Any], correlation_id: int | None
    ) -> None:
        pending = self.pending_configuration
        if (
            not isinstance(pending, PendingConfigurationPrepare)
            or correlation_id != pending.message_id
        ):
            self.events.put(log_event("忽略了非预期的配置保存响应", LogLevel.WARNING))
            return
        try:
            value = PreparedConfiguration.from_wire(prepared)
            if (
                value.project_revision != pending.project_revision
                or value.expected_source_digest != pending.source_digest
            ):
                raise ValueError("prepared configuration identity does not match the request")
            if blake3.blake3(value.contents.encode()).digest() != value.prepared_source_digest:
                raise ValueError("prepared configuration digest does not match its contents")
            if self.bundle is None:
                raise RuntimeError("配置保存完成时项目已关闭")
            if pending.session_only:
                if value.restart_required:
                    raise PermissionError("项目文件仅支持当前会话内即时生效的设置")
                candidate = None
            else:
                candidate = self._write_configuration(
                    self.bundle,
                    value.expected_source_digest,
                    value.contents,
                )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            message = f"保存偏好选项失败：{error}"
            finalize_message_id = self.send_runtime(26, {0: pending.message_id, 1: 0})
            self.pending_configuration = PendingConfigurationFinalize(
                finalize_message_id,
                pending.message_id,
                pending.restart,
                PreparedConfigurationAbort(message),
                pending.automatic,
            )
            self.events.put(FrontendEvent("configuration_save_failed", message))
            return
        finalize_message_id = self.send_runtime(26, {0: pending.message_id, 1: 1})
        self.pending_configuration = PendingConfigurationFinalize(
            finalize_message_id,
            pending.message_id,
            pending.restart,
            PreparedConfigurationCommit(value, candidate),
            pending.automatic,
        )

    def _write_configuration(
        self, bundle: ProjectBundle, expected_digest: bytes, contents: str
    ) -> ProjectBundle:
        if bundle.project_file is None:
            bundle.write_configuration(expected_digest, contents)
            return ProjectBundle.scan_quick(bundle.root, 1, self._project_scan_progress)
        bundle.write_configuration(
            expected_digest,
            contents,
            self.abi.prepare_project_configuration_update,
        )
        project_bytes = bundle.project_file.read_bytes()
        manifest = self.abi.project_file_manifest(project_bytes)
        return ProjectBundle.from_project_file_manifest(bundle.project_file, manifest)

    def _handle_configuration_committed(
        self, committed: dict[int, Any], correlation_id: int | None
    ) -> None:
        pending = self.pending_configuration
        if (
            not isinstance(pending, PendingConfigurationFinalize)
            or correlation_id != pending.finalize_message_id
        ):
            self.events.put(log_event("忽略了非预期的配置提交响应", LogLevel.WARNING))
            return
        self.pending_configuration = None
        automatic_abort = pending.automatic and isinstance(
            pending.outcome, PreparedConfigurationAbort
        )
        snapshot = self._publish_configuration(
            committed.get(0), confirm_generated=not automatic_abort
        )
        if snapshot is None:
            self.events.put(
                FrontendEvent(
                    "configuration_save_failed",
                    "Runtime 返回了无效的配置提交响应",
                )
            )
            return
        if isinstance(pending.outcome, PreparedConfigurationAbort):
            if pending.automatic:
                self.pending_start_after_configuration = None
                self.configuration_snapshot = None
                self.events.put(
                    FrontendEvent(
                        "error",
                        "确认 reraconfig.toml 迁移失败；请重新加载项目",
                    )
                )
            return
        outcome = pending.outcome
        if pending.automatic:
            cache_hit = self.pending_start_after_configuration
            self.pending_start_after_configuration = None
            if cache_hit is not None:
                self._begin_client_preferences(cache_hit)
            return
        if outcome.candidate is None:
            self.events.put(FrontendEvent("configuration_session_applied"))
        else:
            self.bundle = outcome.candidate
            self.events.put(
                FrontendEvent(
                    "configuration_saved",
                    (pending.restart, snapshot.restart_pending),
                )
            )
        if not pending.restart:
            return
        if outcome.candidate is None:
            raise RuntimeError("会话设置不能通过重启应用")
        try:
            project_file_bytes = (
                outcome.candidate.project_file.read_bytes()
                if outcome.candidate.project_file is not None
                else None
            )
            if project_file_bytes is None:
                self.recreate(outcome.candidate)
            else:
                self.recreate(outcome.candidate, project_file_bytes=project_file_bytes)
        except Exception as error:  # noqa: BLE001 - preserve the worker after a restart failure
            self.events.put(FrontendEvent("error", f"偏好选项已保存，但重启游戏失败：{error}"))

    def _submit_project(self, cache_transfer_id: int | None) -> None:
        if self.pending_bundle is None:
            return
        started = time.monotonic_ns()
        self.events.put(FrontendEvent("status", "正在提交项目并编译脚本…"))
        request: dict[int, Any] = {0: self.pending_bundle.identity()}
        if self.pending_bundle.is_materialized:
            manifest = self.pending_bundle.manifest()
            stage_manifest = getattr(self.abi, "stage_project_manifest", None)
            if stage_manifest is None or not stage_manifest(manifest):
                request[1] = manifest
        if cache_transfer_id is not None:
            request[2] = cache_transfer_id
        self.send_runtime(19, request)
        self.record_host_duration("submission_transfer_ms", started)
