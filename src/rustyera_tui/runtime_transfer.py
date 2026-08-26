"""Private RuntimeClient responsibilities extracted from the compatibility facade."""

from __future__ import annotations

from .runtime_dependencies import (
    Any,
    AtomicExportStream,
    COMPILED_CACHE_RETRY_NS,
    Callable,
    DIAGNOSIS_EXPORT_STAGES,
    DIAGNOSIS_PROGRESS_STAGE_BY_EXPORT,
    DiagnosisExport,
    ExportStage,
    FrontendEvent,
    FullProjectExport,
    LogLevel,
    PendingStateImport,
    Path,
    RUNTIME_EXPORT_KIND,
    SNAPSHOT_INELIGIBLE_REASONS,
    STATE_EXPORT_CHUNK_BYTES,
    blake3,
    diagnosis_project_name,
    emit_startup_milestone,
    enum_list_text,
    log_event,
    time,
    unwrap_variant,
    variant,
    _PendingExport,
)


class _RuntimeTransferMixin:
    def _retire_transfers_for_game_transition(
        self, *, reschedule_cache: bool = True
    ) -> None:
        """Release frontend transfer state invalidated by a VM replacement."""

        import_active = self.pending_import is not None
        if import_active:
            self._clear_pending_import()
        export_kind = (
            self.pending_export.stage if self.pending_export is not None else None
        )
        diagnosis_active = self.pending_diagnosis is not None
        if self.pending_export is not None:
            self.pending_export.cancel()
            self.pending_export = None
        project_export_active = self.full_project_export is not None
        if project_export_active:
            self.full_project_export.stream.cancel()
            self.full_project_export = None
        self.pending_cache_after = None
        self.cache_preparation_started = False
        diagnosis = self.pending_diagnosis
        self.pending_diagnosis = None
        if diagnosis is not None:
            diagnosis.cleanup()
        if export_kind == ExportStage.SNAPSHOT:
            self.events.put(FrontendEvent("snapshot_export_finished", False))
        elif export_kind == ExportStage.INPUT_REPLAY:
            self.events.put(FrontendEvent("input_replay_export_finished", False))
        elif export_kind == ExportStage.PROJECT_FILE or project_export_active:
            self.events.put(FrontendEvent("project_file_export_finished", False))
            self.events.put(FrontendEvent("project_progress_finished"))
        elif export_kind in DIAGNOSIS_EXPORT_STAGES or diagnosis_active:
            self.events.put(
                FrontendEvent(
                    "diagnosis_export_finished",
                    (False, "游戏状态切换已取消诊断导出"),
                )
            )
        if export_kind == ExportStage.COMPILED_CACHE and reschedule_cache:
            self.cache_refresh_pending = self.bundle is not None
            self.cache_refresh_after = "background"
            self.cache_refresh_after_ns = time.monotonic_ns() + COMPILED_CACHE_RETRY_NS
        elif export_kind == ExportStage.COMPILED_CACHE:
            self.cache_refresh_pending = False
        if not reschedule_cache:
            self.cache_refresh_pending = False
        self.pending_restore = None

    def _begin_import(self, payload: bytes, kind: int, purpose: str) -> None:
        if self.pending_import is not None:
            self._cancel_pending_import()
        pending = PendingStateImport(kind, purpose, len(payload), payload=payload)
        self.pending_import = pending
        try:
            message_id = self.send_runtime(
                62,
                {0: kind, 1: len(payload), 2: blake3.blake3(payload).digest()},
            )
        except Exception:
            self._clear_pending_import()
            raise
        pending.begin_message_id = message_id
        pending.command_message_ids.add(message_id)

    def _begin_file_import(
        self,
        path: Path,
        size: int,
        kind: int,
        purpose: str,
        *,
        delete_when_finished: bool = True,
    ) -> None:
        if self.pending_import is not None:
            self._cancel_pending_import()
        pending = PendingStateImport(
            kind,
            purpose,
            size,
            path=path,
            delete_path_when_finished=delete_when_finished,
        )
        self.pending_import = pending
        try:
            begin: dict[int, object] = {0: kind, 1: size}
            if kind != 5:
                hasher = blake3.blake3()
                with path.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        hasher.update(chunk)
                begin[2] = hasher.digest()
            message_id = self.send_runtime(62, begin)
        except Exception:
            self._clear_pending_import()
            raise
        pending.begin_message_id = message_id
        pending.command_message_ids.add(message_id)

    def _stage_project_cache(self, payload: bytes, purpose: str) -> None:
        stage = getattr(self.abi, "stage_compiled_cache", None)
        transfer_id = stage(payload) if stage is not None else None
        if transfer_id is None:
            self._begin_import(payload, 2, purpose)
            return
        self._submit_project(transfer_id)

    def _stage_project_cache_file(self, path: Path, purpose: str) -> None:
        stage_file = getattr(self.abi, "stage_compiled_cache_file", None)
        transfer_id = stage_file(path) if stage_file is not None else None
        if transfer_id is None:
            self._stage_project_cache(path.read_bytes(), purpose)
            return
        self._submit_project(transfer_id)

    def _stage_persistent_cache_or_source(self) -> None:
        if self.pending_bundle is not None and self.pending_bundle.project_file is not None:
            self.events.put(FrontendEvent("status", "正在载入项目文件…"))
            self._stage_project_cache_file(self.pending_bundle.project_file, "project_file")
            return
        cache_read_started = time.monotonic_ns()
        cache_path = (
            self.storage.compiled_cache_path()
            if self.storage and self.allow_compiled_cache_load
            else None
        )
        if cache_path is not None:
            try:
                if cache_path.stat().st_size > 0:
                    self.events.put(FrontendEvent("status", "正在载入项目缓存…"))
                    self._stage_project_cache_file(cache_path, "project_cache")
                    self.record_host_duration("cache_read_ms", cache_read_started)
                    return
            except OSError as error:
                self.events.put(log_event(f"读取项目缓存失败：{error}", LogLevel.WARNING))
        self.record_host_duration("cache_read_ms", cache_read_started)
        if self.pending_bundle is None:
            return
        started = time.monotonic_ns()
        self.pending_bundle = self.pending_bundle.materialize(self._project_scan_progress)
        self.record_host_duration("source_materialize_ms", started)
        self._submit_project(None)

    def _refresh_compiled_cache(self, after: str) -> None:
        self.cache_refresh_pending = False
        self.cache_ready = False
        self.pending_cache_after = after
        if self.storage is None:
            self._finish_cache_export(False)
            return
        cache_path = self.storage.compiled_cache_path()
        self.pending_export = _PendingExport.open(
            cache_path, ExportStage.COMPILED_CACHE
        )
        if not self.cache_preparation_started:
            self.cache_preparation_started = True
            self.events.put(
                FrontendEvent(
                    "status",
                    "正在后台生成项目缓存，可继续游戏，但游戏运行和响应速度可能暂时受到影响…",
                )
            )
        self.pending_export.message_id = self.send_runtime(60, {0: 2, 1: 0})

    def maybe_refresh_compiled_cache(self) -> None:
        if self.full_project_export is not None:
            if (
                self.pending_import is None
                and self.pending_export is None
                and time.monotonic_ns() >= self.full_project_export.retry_after_ns
            ):
                self._request_project_file_export()
            return
        if self.pending_diagnosis is not None:
            if self.pending_diagnosis.stage == "export_wait" and self.pending_export is None:
                self._start_diagnosis_replay_export()
            elif (
                self.pending_diagnosis.stage == "project_wait"
                and self.pending_export is None
                and time.monotonic_ns() >= self.pending_diagnosis.retry_after_ns
            ):
                self._request_diagnosis_project_export()
            return
        if (
            self.cache_refresh_pending
            and self.pending_export is None
            and time.monotonic_ns() >= self.cache_refresh_after_ns
        ):
            self._refresh_compiled_cache(self.cache_refresh_after)

    def defer_compiled_cache_refresh(self) -> None:
        from . import runtime as runtime_facade

        """Keep cache compression out of latency-sensitive gameplay transitions."""

        if self.cache_refresh_pending:
            self.cache_refresh_after_ns = max(
                self.cache_refresh_after_ns,
                time.monotonic_ns() + runtime_facade.COMPILED_CACHE_PERSIST_DELAY_NS,
            )

    def export_snapshot(self, path: Path, purpose: str) -> None:
        purpose_id = {"normal": 0, "debug": 1}.get(purpose)
        if purpose_id is None:
            raise ValueError(f"unknown snapshot export purpose {purpose}")
        self._start_buffered_state_export(path, ExportStage.SNAPSHOT, 1, purpose_id)

    def export_input_replay(self, path: Path) -> None:
        self._start_buffered_state_export(path, ExportStage.INPUT_REPLAY, 4, 0)

    def _start_buffered_state_export(
        self, path: Path, stage: ExportStage, runtime_kind: int, purpose: int
    ) -> None:
        if self.pending_export is not None:
            raise RuntimeError("another state export is already active")
        pending = _PendingExport.open(path, stage)
        self.pending_export = pending
        try:
            pending.message_id = self.send_runtime(60, {0: runtime_kind, 1: purpose})
        except Exception:
            pending.cancel()
            self.pending_export = None
            raise

    def export_project_file(self, path: Path, cancelled: Callable[[], bool] | None = None) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        if (
            self.pending_export is not None
            and self.pending_export.stage == ExportStage.COMPILED_CACHE
        ) or self.cache_preparation_started:
            self.send_runtime(71, {0: 2})
            if self.pending_export is not None:
                self.pending_export.cancel()
            self.pending_export = None
            self.cache_preparation_started = False
            self.cache_refresh_pending = True
            self.cache_refresh_after_ns = time.monotonic_ns() + COMPILED_CACHE_RETRY_NS
        self.full_project_export = FullProjectExport(path, AtomicExportStream.open(path))
        try:
            self._stage_full_project_manifest("full_project_export", cancelled)
        except Exception:
            self._finish_project_file_export(False)
            raise

    def _stage_full_project_manifest(
        self, purpose: str, cancelled: Callable[[], bool] | None = None
    ) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        if self.bundle.project_file is None:
            path, size = self.bundle.write_full_manifest_temp(
                self._project_scan_progress, cancelled
            )
            self._begin_file_import(path, size, 5, purpose)
        elif purpose == "full_project_export":
            self._request_project_file_export()
        else:
            self._request_diagnosis_project_export()

    def _request_project_file_export(self) -> None:
        export = self.full_project_export
        if export is None:
            return
        pending = _PendingExport(export.target, ExportStage.PROJECT_FILE, export.stream)
        self.pending_export = pending
        pending.message_id = self.send_runtime(60, {0: 3, 1: 0})

    def cancel_project_file_export(self) -> None:
        if (
            (
                self.pending_export is None
                or self.pending_export.stage != ExportStage.PROJECT_FILE
            )
            and self.full_project_export is None
        ):
            return
        self.send_runtime(71, {0: 3})
        self._finish_project_file_export(None, "已取消导出全量项目文件")

    def export_diagnosis(self, path: Path, logs: str, project_name: str) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        if self.pending_diagnosis is not None:
            raise RuntimeError("another diagnosis export is already active")
        self.pending_diagnosis = DiagnosisExport.create(
            path,
            diagnosis_project_name(project_name),
            logs,
        )
        self._report_diagnosis_progress("waiting")
        if self.pending_export is None:
            try:
                self._start_diagnosis_replay_export()
            except Exception:
                if self.pending_diagnosis is not None:
                    self._finish_diagnosis_export(
                        False, "无法创建诊断操作序列临时文件"
                    )
                raise

    def _start_diagnosis_replay_export(self) -> None:
        diagnosis = self.pending_diagnosis
        if diagnosis is None:
            return
        diagnosis.stage = "replay"
        self._report_diagnosis_progress("input_replay")
        pending = _PendingExport.open(
            diagnosis.part_path("input-replay.jsonl"), ExportStage.DIAGNOSIS_REPLAY
        )
        self.pending_export = pending
        try:
            pending.message_id = self.send_runtime(60, {0: 4, 1: 0})
        except Exception:
            self._finish_diagnosis_export(False, "无法开始导出诊断操作序列")
            raise

    def _start_diagnosis_snapshot_export(self) -> None:
        diagnosis = self.pending_diagnosis
        if diagnosis is None:
            return
        diagnosis.stage = "snapshot"
        self._report_diagnosis_progress("vm_snapshot")
        pending = _PendingExport.open(
            diagnosis.part_path("runtime.snapshot"), ExportStage.DIAGNOSIS_SNAPSHOT
        )
        self.pending_export = pending
        try:
            pending.message_id = self.send_runtime(60, {0: 1, 1: 2})
        except Exception:
            self._finish_diagnosis_export(False, "无法开始导出诊断快照")
            raise

    def _handle_export_ready(
        self, ready: dict[int, Any], correlation_id: int | None = None
    ) -> None:
        if self.pending_export is None:
            return
        pending = self.pending_export
        stage = pending.stage
        expected_kind = RUNTIME_EXPORT_KIND.get(stage)
        if (
            expected_kind is None
            or (
                pending.message_id is not None
                and correlation_id != pending.message_id
            )
            or ready.get(0) != expected_kind
        ):
            self._fail_mismatched_export("state export ready does not match the active request")
            return
        pending.message_id = None
        result_tag, fields = unwrap_variant(ready[1])
        if result_tag == 1:
            reasons = enum_list_text(
                fields[0], SNAPSHOT_INELIGIBLE_REASONS, "SnapshotIneligibleReason"
            )
            label = (
                "导出操作序列"
                if stage == ExportStage.INPUT_REPLAY
                else "生成快照"
            )
            self.events.put(FrontendEvent("runtime_error", f"当前状态不能{label}：{reasons}"))
            pending.cancel()
            self.pending_export = None
            if stage == ExportStage.COMPILED_CACHE:
                self._finish_cache_export(False)
            elif stage == ExportStage.PROJECT_FILE:
                self._finish_project_file_export(False)
            elif stage in DIAGNOSIS_EXPORT_STAGES:
                self._finish_diagnosis_export(False, f"当前状态不能导出：{reasons}")
            elif stage == ExportStage.INPUT_REPLAY:
                self.events.put(FrontendEvent("input_replay_export_finished", False))
            else:
                self.events.put(FrontendEvent("snapshot_export_finished", False))
            return
        descriptor = fields[0]
        if descriptor.get(1) != expected_kind:
            self._fail_mismatched_export("state export descriptor kind does not match the request")
            return
        pending.descriptor = descriptor
        if stage in DIAGNOSIS_EXPORT_STAGES:
            self._report_diagnosis_progress(
                DIAGNOSIS_PROGRESS_STAGE_BY_EXPORT[stage],
                0,
                int(descriptor[2]),
            )
        self.send_runtime(67, {0: descriptor[0], 1: 0, 2: STATE_EXPORT_CHUNK_BYTES})

    def _handle_export_chunk(self, chunk: dict[int, Any]) -> None:
        from . import runtime as runtime_facade

        if self.pending_export is None:
            return
        pending = self.pending_export
        path = pending.path
        descriptor = pending.descriptor
        stream = pending.stream
        stage = pending.stage
        received = stream.received
        if descriptor is None or chunk[0] != descriptor[0] or chunk[1] != received:
            if stage in DIAGNOSIS_EXPORT_STAGES:
                self._finish_diagnosis_export(
                    False, "diagnosis state export chunk is out of sequence"
                )
                return
            raise RuntimeError("snapshot export chunk is out of sequence")
        payload = chunk[2]
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            self._fail_mismatched_export("state export chunk payload is not bytes")
            return
        stream.write(payload)
        if stage in DIAGNOSIS_EXPORT_STAGES:
            self._report_diagnosis_progress(
                DIAGNOSIS_PROGRESS_STAGE_BY_EXPORT[stage],
                received + len(payload),
                int(descriptor[2]),
            )
        if not chunk[3]:
            self.send_runtime(
                67,
                {0: descriptor[0], 1: received + len(payload), 2: STATE_EXPORT_CHUNK_BYTES},
            )
            return
        try:
            exported_path = pending.finish()
        except Exception as error:
            if stage in DIAGNOSIS_EXPORT_STAGES:
                self._finish_diagnosis_export(False, str(error))
                return
            self.pending_export = None
            raise
        self.pending_export = None
        if stage == ExportStage.COMPILED_CACHE:
            self._finish_cache_export(True)
        elif stage == ExportStage.PROJECT_FILE:
            if self.full_project_export is None:
                raise RuntimeError("project export stream is missing")
            self._finish_project_file_export(True, f"项目文件已导出到 {path}")
        elif stage == ExportStage.DIAGNOSIS_SNAPSHOT:
            if self.pending_diagnosis is None:
                raise RuntimeError("diagnosis export state is missing")
            self.pending_diagnosis.snapshot = exported_path
            self._start_diagnosis_project_export()
        elif stage == ExportStage.DIAGNOSIS_REPLAY:
            if self.pending_diagnosis is None:
                raise RuntimeError("diagnosis export state is missing")
            self.pending_diagnosis.input_replay = exported_path
            self._start_diagnosis_snapshot_export()
        elif stage == ExportStage.DIAGNOSIS_PROJECT:
            if self.pending_diagnosis is None:
                raise RuntimeError("diagnosis export state is missing")
            self.pending_diagnosis.project_file = exported_path
            if (
                self.pending_diagnosis.input_replay is None
                or self.pending_diagnosis.snapshot is None
            ):
                self._finish_diagnosis_export(False, "diagnosis archive input is missing")
                return
            try:
                self._report_diagnosis_progress("archive")
                runtime_facade.write_diagnosis_archive(
                    self.pending_diagnosis.target,
                    project_name=self.pending_diagnosis.project_name,
                    snapshot=self.pending_diagnosis.snapshot,
                    input_replay=self.pending_diagnosis.input_replay,
                    logs=self.pending_diagnosis.logs,
                    project_file=self.pending_diagnosis.project_file,
                    progress=lambda completed, total: self._report_diagnosis_progress(
                        "archive", completed, total
                    ),
                )
            except Exception as error:  # noqa: BLE001 - report filesystem/compression failures
                self._finish_diagnosis_export(False, str(error))
            else:
                self._finish_diagnosis_export(True, str(self.pending_diagnosis.target))
        elif stage == ExportStage.INPUT_REPLAY:
            self.events.put(FrontendEvent("input_replay_export_finished", True))
            self.events.put(FrontendEvent("status", f"操作序列已导出到 {path}"))
        else:
            self.events.put(FrontendEvent("snapshot_export_finished", True))
            self.events.put(FrontendEvent("status", f"VM 快照已导出到 {path}"))

    def _start_diagnosis_project_export(self) -> None:
        diagnosis = self.pending_diagnosis
        if diagnosis is None:
            return
        diagnosis.stage = "project"
        self._report_diagnosis_progress("project_scanning")
        try:
            self._stage_full_project_manifest("diagnosis_project_export")
        except Exception as error:  # noqa: BLE001 - report project scan failures to the UI
            self._finish_diagnosis_export(False, str(error))
            return

    def _request_diagnosis_project_export(self) -> None:
        diagnosis = self.pending_diagnosis
        if diagnosis is None:
            return
        pending = _PendingExport.open(
            diagnosis.part_path("project.reraproj"), ExportStage.DIAGNOSIS_PROJECT
        )
        self.pending_export = pending
        try:
            pending.message_id = self.send_runtime(60, {0: 3, 1: 0})
        except Exception:
            self._finish_diagnosis_export(False, "无法开始导出诊断项目文件")
            raise

    def _finish_diagnosis_export(self, success: bool, message: str) -> None:
        pending = self.pending_export
        if not success and self.pending_import is not None:
            self._cancel_pending_import()
        if not success:
            descriptor = pending.descriptor if pending is not None else None
            try:
                if descriptor is not None:
                    self.send_runtime(69, {0: descriptor[0]})
                elif pending is not None and pending.stage in DIAGNOSIS_EXPORT_STAGES:
                    self.send_runtime(71, {0: RUNTIME_EXPORT_KIND[pending.stage]})
            except Exception:  # noqa: BLE001 - cleanup must restore frontend interaction
                pass
        if pending is not None:
            pending.cancel()
        self.pending_export = None
        diagnosis = self.pending_diagnosis
        self.pending_diagnosis = None
        if diagnosis is not None:
            diagnosis.cleanup()
        self.events.put(FrontendEvent("diagnosis_export_finished", (success, message)))

    def _fail_mismatched_export(self, message: str) -> None:
        pending = self.pending_export
        if pending is not None and pending.stage in DIAGNOSIS_EXPORT_STAGES:
            self._finish_diagnosis_export(False, message)
            return
        if pending is not None:
            pending.cancel()
            self.pending_export = None
        raise RuntimeError(message)

    def _finish_cache_export(self, success: bool) -> None:
        if not success and self.pending_export is not None:
            self.pending_export.cancel()
        self.pending_export = None
        after = self.pending_cache_after
        self.pending_cache_after = None
        self.cache_preparation_started = False
        if success and self.storage is not None:
            emit_startup_milestone(
                "cache_persisted",
                attempt_id=self.startup_attempt,
                scenario=self.startup_scenario,
            )
            for obsolete in self.storage.obsolete_compiled_cache_paths():
                try:
                    obsolete.unlink(missing_ok=True)
                except OSError as error:
                    self.events.put(log_event(f"删除旧编译缓存失败：{error}", LogLevel.WARNING))
        if after == "start":
            self.events.put(
                FrontendEvent(
                    "status",
                    "项目缓存已保存，正在进入标题画面…"
                    if success
                    else "项目缓存保存失败，正在进入标题画面…",
                )
            )
            self._submit_start(self._new_game_start())
        elif after == "reload":
            self.events.put(
                FrontendEvent("status", "项目缓存已保存。" if success else "项目缓存保存失败。")
            )
        elif after == "background":
            self.events.put(
                FrontendEvent("status", "项目缓存已保存。" if success else "项目缓存保存失败。")
            )

    def _finish_project_file_export(self, success: bool | None, status: str | None = None) -> None:
        if success is not True and self.pending_import is not None:
            self._cancel_pending_import()
        export = self.full_project_export
        if success is not True and export is not None:
            export.stream.cancel()
        if self.pending_export is not None:
            self.pending_export.cancel()
        self.full_project_export = None
        self.pending_export = None
        self.events.put(FrontendEvent("project_file_export_finished", success))
        self.events.put(FrontendEvent("project_progress_finished"))
        if status is not None:
            self.events.put(FrontendEvent("status", status))

    def _handle_import_accepted(
        self, accepted: dict[int, Any], correlation_id: int | None = None
    ) -> None:
        from . import runtime as runtime_facade

        pending = self.pending_import
        if pending is None:
            self.send_runtime(69, {0: accepted[0]})
            return
        if correlation_id is not None and correlation_id != pending.begin_message_id:
            self.events.put(log_event("忽略了不匹配的状态导入 Accepted", LogLevel.WARNING))
            self.send_runtime(69, {0: accepted[0]})
            return
        if pending.transfer_id is not None:
            self.events.put(log_event("忽略了重复的状态导入 Accepted", LogLevel.WARNING))
            return
        try:
            transfer_id = accepted[0]
            pending.transfer_id = transfer_id
            offset = 0
            hasher = blake3.blake3()
            if pending.path is not None:
                with pending.path.open("rb") as stream:
                    while part := stream.read(runtime_facade.FULL_PROJECT_MANIFEST_CHUNK_BYTES):
                        message_id = self.send_runtime(64, {0: transfer_id, 1: offset, 2: part})
                        pending.command_message_ids.add(message_id)
                        hasher.update(part)
                        offset += len(part)
                if pending.delete_path_when_finished:
                    try:
                        pending.path.unlink(missing_ok=True)
                    except OSError as error:
                        self.events.put(
                            log_event(
                                f"删除状态导入临时文件失败：{error}",
                                LogLevel.WARNING,
                            )
                        )
                pending.path = None
                if offset != pending.total_bytes:
                    raise RuntimeError("full project manifest changed while being transferred")
                commit: dict[int, object] = {0: transfer_id}
                if pending.kind == 5:
                    commit[1] = hasher.digest()
                message_id = self.send_runtime(65, commit)
                pending.commit_message_id = message_id
                pending.command_message_ids.add(message_id)
                return
            assert pending.payload is not None
            while offset < len(pending.payload):
                part = pending.payload[offset : offset + runtime_facade.STATE_IMPORT_CHUNK_BYTES]
                message_id = self.send_runtime(64, {0: transfer_id, 1: offset, 2: part})
                pending.command_message_ids.add(message_id)
                offset += len(part)
            message_id = self.send_runtime(65, {0: transfer_id})
            pending.commit_message_id = message_id
            pending.command_message_ids.add(message_id)
        except Exception as error:  # noqa: BLE001 - import failure must clean host/runtime state
            self._fail_pending_import(f"状态导入传输失败：{error}")

    def _handle_import_ready(
        self, ready: dict[int, Any], correlation_id: int | None = None
    ) -> None:
        pending = self.pending_import
        if (
            pending is None
            or pending.transfer_id != ready[0]
            or ready.get(1) != pending.kind
            or (correlation_id is not None and correlation_id != pending.commit_message_id)
        ):
            self.events.put(log_event("忽略了不匹配的状态导入 Ready", LogLevel.WARNING))
            return
        purpose = pending.purpose
        if purpose == "full_project_export":
            self._request_project_file_export()
        elif purpose == "diagnosis_project_export":
            self._request_diagnosis_project_export()
        elif purpose in {"project_cache", "project_file"}:
            self._submit_project(ready[0])
        elif purpose == "traditional_save":
            self.events.put(FrontendEvent("status", "传统存档传输完成，正在读档…"))
            self._clear_pending_import()
            self.pending_restore = None
            message_id = self._submit_start({0: variant(1, ready[0])})
            self.begin_game_state_transition(message_id)
            return
        else:
            self.events.put(FrontendEvent("status", "快照传输完成，正在恢复 VM…"))
            self._clear_pending_import()
            self.pending_restore = None
            message_id = self._submit_start({0: variant(2, ready[0])})
            self.begin_game_state_transition(message_id)
            return
        self._clear_pending_import()
        self.pending_restore = None

    def _clear_pending_import(self) -> None:
        pending = self.pending_import
        self.pending_import = None
        if (
            pending is not None
            and pending.path is not None
            and pending.delete_path_when_finished
        ):
            try:
                pending.path.unlink(missing_ok=True)
            except OSError as error:
                self.events.put(
                    log_event(f"删除状态导入临时文件失败：{error}", LogLevel.WARNING)
                )

    def _cancel_pending_import(self) -> None:
        pending = self.pending_import
        self._clear_pending_import()
        if pending is not None and pending.transfer_id is not None:
            try:
                self.send_runtime(69, {0: pending.transfer_id})
            except Exception:  # noqa: BLE001 - cleanup must not mask the primary failure
                pass

    def _fail_pending_import(self, message: str) -> None:
        pending = self.pending_import
        if pending is None:
            return
        purpose = pending.purpose
        self._cancel_pending_import()
        if purpose == "full_project_export":
            self._finish_project_file_export(False, message)
        elif purpose == "diagnosis_project_export":
            self._finish_diagnosis_export(False, message)
        else:
            self.pending_restore = None
            self.events.put(FrontendEvent("runtime_error", message))

    def restore_snapshot(self, path: Path) -> None:
        resolved = path.expanduser().resolve(strict=True)
        self.pending_restore = (resolved, None, "snapshot")
        self._begin_file_import(
            resolved,
            resolved.stat().st_size,
            1,
            "snapshot",
            delete_when_finished=False,
        )

    def restore_save(self, path: Path) -> None:
        resolved = path.expanduser().resolve(strict=True)
        self.pending_restore = (resolved, None, "traditional_save")
        self._begin_file_import(
            resolved,
            resolved.stat().st_size,
            0,
            "traditional_save",
            delete_when_finished=False,
        )
