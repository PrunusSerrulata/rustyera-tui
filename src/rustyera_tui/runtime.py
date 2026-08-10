"""Caller-pumped runtime client and its frontend worker thread."""

from __future__ import annotations

import copy
import os
import queue
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import blake3
from rich.cells import cell_len

from .abi import RuntimeAbi
from .configuration import (
    APPLICATION_HOT,
    ConfigurationChange,
    ConfigurationSnapshot,
    PreparedConfiguration,
)
from .diagnosis import diagnosis_project_name, write_diagnosis_archive
from .image_metadata import decode_image_metadata
from .input_policy import is_message_skip_wait, is_message_wait, message_wait_intent
from .log_model import LogLevel
from .presentation import (
    ServicePresentationModel,
    coalesce_presentation_deltas,
    html_printed_str,
    plain_line,
)
from .project import ProjectBundle, StorageBackend
from .protocol_text import (
    COMMAND_ERROR_CODES,
    SERVICE_KINDS,
    SNAPSHOT_INELIGIBLE_REASONS,
    enum_list_text,
    enum_text,
)
from .runtime_support import (
    RuntimeFailure as RuntimeFailure,
    format_project_diagnostic as format_project_diagnostic,
    log_event as log_event,
    runtime_log_level as runtime_log_level,
)
from .startup_telemetry import emit_startup_milestone
from .runtime_types import (
    FrontendCommand as FrontendCommand,
    FrontendEvent as FrontendEvent,
    PresentationBatch as PresentationBatch,
)
from .wire import (
    CHANNEL_DEBUG,
    CHANNEL_RUNTIME,
    DEBUG_VERSION,
    RUNTIME_VERSION,
    debug_message,
    decode,
    decode_envelope,
    encode,
    encode_envelope,
    message_value,
    runtime_message,
    unwrap_variant,
    variant,
    version_range,
)
from .worker import RuntimeWorker as RuntimeWorker

COMPILED_CACHE_PERSIST_DELAY_NS = 10_000_000_000
COMPILED_CACHE_RETRY_NS = 250_000_000
STATE_IMPORT_CHUNK_BYTES = 16 * 1024 * 1024


def _debug_action_owner(action: str) -> str | None:
    if action.startswith("console_"):
        return "console"
    if action in {"variables", "read_variable"}:
        return "variables"
    if action in {"fibers", "call_stack"}:
        return "stack"
    return None


@dataclass(slots=True)
class DiagnosisExport:
    target: Path
    project_name: str
    logs: str
    snapshot: bytes | None = None
    compiled_artifact: bytes | None = None
    stage: str = "snapshot"
    retry_after_ns: int = 0


@dataclass(slots=True)
class AtomicExportStream:
    target: Path
    temporary: Path
    stream: Any
    hasher: Any
    received: int = 0

    @classmethod
    def open(cls, target: Path) -> AtomicExportStream:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        return cls(target, Path(temporary), os.fdopen(descriptor, "wb"), blake3.blake3())

    def write(self, data: bytes) -> None:
        if self.stream.write(data) != len(data):
            raise OSError("project export was not written completely")
        self.hasher.update(data)
        self.received += len(data)

    def finish(self, expected_size: int, expected_digest: bytes) -> None:
        if self.received != expected_size or self.hasher.digest() != expected_digest:
            raise RuntimeError("project export digest verification failed")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        os.replace(self.temporary, self.target)

    def cancel(self) -> None:
        if not self.stream.closed:
            self.stream.close()
        self.temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class FullProjectExport:
    target: Path
    stream: AtomicExportStream
    retry_after_ns: int = 0


@dataclass(slots=True)
class PendingGameInput:
    wait: dict[int, Any]
    intent: list[Any]
    message_skip: bool
    stale_retries: int = 0


@dataclass(frozen=True, slots=True)
class PendingConfigurationPrepare:
    message_id: int
    project_revision: int
    source_digest: bytes
    restart: bool
    session_only: bool = False
    automatic: bool = False


@dataclass(frozen=True, slots=True)
class PreparedConfigurationCommit:
    prepared: PreparedConfiguration
    candidate: ProjectBundle | None


@dataclass(frozen=True, slots=True)
class PreparedConfigurationAbort:
    error: str


@dataclass(frozen=True, slots=True)
class PendingConfigurationFinalize:
    finalize_message_id: int
    preparation_message_id: int
    restart: bool
    outcome: PreparedConfigurationCommit | PreparedConfigurationAbort
    automatic: bool = False


class RuntimeClient:
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
        self.storage: StorageBackend | None = None
        self.presentation = ServicePresentationModel()
        self.active_wait: dict[int, Any] | None = None
        self.pending_restore: tuple[Path, bytes, str] | None = None
        self.pending_export: tuple[Path, bytearray, dict[int, Any] | None] | None = None
        self.pending_export_message: int | None = None
        self.pending_diagnosis: DiagnosisExport | None = None
        self.import_bytes: bytes | None = None
        self.import_transfer_id: int | None = None
        self.import_purpose: str | None = None
        self.pending_export_kind: int | None = None
        self.pending_cache_after: str | None = None
        self.pending_cache_export_message: int | None = None
        self.cache_refresh_pending = False
        self.cache_ready = False
        self.cache_refresh_after_ns = 0
        self.cache_preparation_started = False
        self.pending_cache_stream: AtomicExportStream | None = None
        self.allow_compiled_cache_load = True
        self.pending_project_file_bytes: bytes | None = None
        self.full_project_export: FullProjectExport | None = None
        self.pending_configuration: (
            PendingConfigurationPrepare | PendingConfigurationFinalize | None
        ) = None
        self.pending_start_after_configuration: bool | None = None
        self.configuration_snapshot: ConfigurationSnapshot | None = None
        self.configuration_profile_supported = False
        self.debug_requested = False
        self.debug_grant: dict[int, Any] | None = None
        self.stop_token: dict[int, Any] | None = None
        self.selected_fiber: int | None = None
        self.pending_debug_actions: list[tuple[str, Any]] = []
        self.debug_pending_by_message: dict[int, str] = {}
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
        self._pending_presentation_events: list[tuple[str, dict[int, Any]]] = []
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        self._projection_messages: set[int] = set()
        self._input_messages: dict[int, PendingGameInput] = {}
        self._send_hello()

    def _project_scan_progress(self, completed: int, total: int) -> None:
        self.events.put(FrontendEvent("project_progress", (0, completed, total)))

    def _reset_wire_state(self) -> None:
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
        self.single_step_enabled = False
        self.debug_step_in_flight = False
        self.debug_disable_pending = False
        self.transient_pause_owner = None
        self.transient_close_pending = None
        self.shutting_down = False
        self._pending_presentation_events.clear()
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        self._projection_messages.clear()
        self._input_messages.clear()
        self.pending_export = None
        self.pending_export_kind = None
        self.pending_export_message = None
        self.pending_diagnosis = None
        self.pending_cache_after = None
        self.pending_cache_export_message = None
        self.cache_refresh_pending = False
        self.cache_ready = False
        self.cache_refresh_after_ns = 0
        self.cache_preparation_started = False
        if self.pending_cache_stream is not None:
            self.pending_cache_stream.cancel()
            self.pending_cache_stream = None
        if self.full_project_export is not None:
            self.full_project_export.stream.cancel()
            self.full_project_export = None
        self.pending_configuration = None
        self.pending_start_after_configuration = None
        self.configuration_snapshot = None
        self.configuration_profile_supported = False

    def recreate(
        self,
        bundle: ProjectBundle,
        restore: tuple[Path, bytes, str] | None = None,
        *,
        allow_compiled_cache: bool = True,
        project_file_bytes: bytes | None = None,
    ) -> None:
        if not self.startup_active:
            self.begin_startup_attempt(project_file=bundle.project_file is not None)
        self.events.put(FrontendEvent("configuration_cleared"))
        self.events.put(FrontendEvent("status", "正在创建新的 Runtime session…"))
        self.abi.recreate_session()
        self._reset_wire_state()
        self.pending_bundle = bundle
        self.pending_restore = restore
        self.allow_compiled_cache_load = allow_compiled_cache
        self.pending_project_file_bytes = project_file_bytes
        self.storage = self._storage_for_bundle(bundle)
        self._send_hello()

    def begin_startup_attempt(self, *, project_file: bool) -> None:
        self.startup_attempt += 1
        self.startup_scenario = "project_file" if project_file else "cold"
        self.startup_active = True
        self.startup_start_submitted = False
        self.startup_first_phase_reported = False
        emit_startup_milestone(
            "attempt_started",
            attempt_id=self.startup_attempt,
            scenario=self.startup_scenario,
        )

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

    def _send_hello(self) -> None:
        service_capabilities = [
            {0: 9, 1: "random_seed", 2: version_range(1, 0)},
            {0: 8, 1: "local_date_time", 2: version_range(1, 0)},
            {0: 7, 1: "get_key_state", 2: version_range(1, 0)},
            {0: 1, 1: "image_metadata", 2: version_range(1, 0)},
            {0: 10, 1: "get_display_line", 2: version_range(1, 0)},
            {0: 10, 1: "html_get_printed_str", 2: version_range(1, 0)},
            {0: 10, 1: "serialize_physical_history", 2: version_range(1, 0)},
            {0: 0, 1: "gget_text_size", 2: version_range(1, 0)},
        ]
        capabilities = {
            0: [0, 1],
            1: True,
            2: True,
            3: False,
            4: False,
            5: False,
            6: True,
            7: True,
            8: True,
            9: [],
            10: service_capabilities,
            11: {0: True, 1: True, 2: True, 3: True},
        }
        maximum_envelope_bytes, maximum_payload_bytes = (
            self.pending_bundle.requested_wire_limits()
            if self.pending_bundle is not None
            else (128 * 1024 * 1024, 127 * 1024 * 1024)
        )
        limits = {
            0: maximum_envelope_bytes,
            1: maximum_payload_bytes,
            2: 128,
            3: 4096,
            4: 1_000_000,
            5: 512 * 1024 * 1024,
        }
        hello = {
            0: version_range(*RUNTIME_VERSION),
            1: "rustyera-textual-tui",
            2: [0, 1, 2, 3, 4, 10, 11, 12, 13, 14],
            3: limits,
            4: capabilities,
            5: ["zh-CN", "ja", "en"],
            6: 1,
        }
        self.send_runtime(0, hello)

    @staticmethod
    def _storage_for_bundle(bundle: ProjectBundle) -> StorageBackend:
        if bundle.project_file is None:
            return StorageBackend(bundle.root)
        return StorageBackend(
            bundle.root,
            data_root=bundle.root / ".rustyera" / "packaged-projects",
            identity_path=bundle.project_file,
        )

    def send_runtime(
        self, tag: int, value: Any | None = None, *, correlation_id: int | None = None
    ) -> int:
        message_id = self.next_message_id
        self.next_message_id += 1
        data = encode_envelope(
            channel=CHANNEL_RUNTIME,
            channel_version=RUNTIME_VERSION,
            session=self.session,
            sequence=self.runtime_sequence,
            message_id=message_id,
            correlation_id=correlation_id,
            payload_tag=tag,
            payload=runtime_message(tag, value),
            epoch=self.epoch,
        )
        self.runtime_sequence += 1
        self.abi.submit(data)
        return message_id

    def send_debug(self, tag: int, value: Any | None = None, *, pending: str = "") -> int:
        if self.session is None or self.epoch is None:
            raise RuntimeError("debug protocol requires an active runtime session")
        message_id = self.next_message_id
        self.next_message_id += 1
        data = encode_envelope(
            channel=CHANNEL_DEBUG,
            channel_version=DEBUG_VERSION,
            session=self.session,
            sequence=self.debug_sequence,
            message_id=message_id,
            correlation_id=None,
            payload_tag=tag,
            payload=debug_message(tag, value),
            epoch=self.epoch,
        )
        self.debug_sequence += 1
        self.abi.submit(data)
        if pending:
            self.debug_pending_by_message[message_id] = pending
        return message_id

    def pump(self) -> bool:
        pump_started = time.perf_counter()
        self._pending_presentation_events.clear()
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        # Sample automatic time only when the next drive is about to start. The worker drains
        # queued user commands before calling pump(), so a user action for the visible wait is
        # submitted before this timer tick instead of racing a tick left queued by the prior
        # presentation batch.
        self._advance_deadline()
        drive_started = time.perf_counter()
        report = self.abi.drive()
        drive_ms = (time.perf_counter() - drive_started) * 1000
        emitted = False
        acknowledge_through: int | None = None
        while data := self.abi.poll():
            emitted = True
            runtime_sequence = self._handle_envelope(data)
            if runtime_sequence is not None:
                acknowledge_through = runtime_sequence
        self._flush_presentation_events()
        # Runtime output acknowledgement is cumulative. Deferring it until the complete poll
        # batch also ensures an epoch-changing reload is acknowledged with its final epoch,
        # even when an earlier message in the same batch was emitted before the commit.
        if acknowledge_through is not None and self.session is not None:
            self.send_runtime(93, {0: acknowledge_through})
        pump_ms = (time.perf_counter() - pump_started) * 1000
        if (
            self.metrics_threshold_ms is not None
            and max(drive_ms, pump_ms) >= self.metrics_threshold_ms
        ):
            self.events.put(
                FrontendEvent(
                    "runtime_metrics",
                    {
                        "drive_ms": drive_ms,
                        "pump_ms": pump_ms,
                        "vm_instructions": report.vm_instructions,
                        "runtime_transitions": report.runtime_transitions,
                        "queued_envelopes": report.queued_envelopes,
                        "state": report.state,
                    },
                )
            )
        return emitted or report.state in (1, 2)

    def _new_game_start(self) -> dict[int, Any]:
        """Build the normal start request, optionally using a deterministic test seed."""

        return {0: variant(0, self.new_game_seed)}

    def _flush_presentation_events(self) -> None:
        snapshot: dict[int, Any] | None = None
        delta: dict[int, Any] | None = None
        if self._pending_presentation_events:
            deltas: list[dict[int, Any]] = []
            for kind, value in self._pending_presentation_events:
                if kind == "snapshot":
                    snapshot = value
                    deltas.clear()
                else:
                    deltas.append(value)
            if deltas:
                delta = coalesce_presentation_deltas(deltas)
        if (
            snapshot is None
            and delta is None
            and not (self._wait_event_dirty or self._presentation_boundary_dirty)
        ):
            return
        # A single queue item prevents Textual from observing presentation and wait halves
        # from different runtime pumps. Intermediate running batches still update its staged
        # model, but only a stable boundary may become a visible frame.
        render = self._presentation_boundary_dirty or self.active_wait is not None
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

    def _handle_envelope(self, data: bytes) -> int | None:
        envelope = decode_envelope(data)
        # A committed new game, restore, or hot reload may advance the epoch before its first
        # StateChanged message is observed. The common envelope already carries that authority;
        # adopt it before acknowledging the message so the acknowledgement cannot be stale.
        if envelope.epoch is not None:
            self.epoch = envelope.epoch
        if envelope.channel == CHANNEL_RUNTIME:
            if envelope.sequence != self.expected_runtime_output:
                raise RuntimeError(
                    f"runtime output sequence gap: expected {self.expected_runtime_output}, "
                    f"received {envelope.sequence}"
                )
            self.expected_runtime_output += 1
            value = message_value(envelope.payload, envelope.payload_tag)
            self._handle_runtime(envelope.payload_tag, value, envelope.correlation_id)
            return envelope.sequence
        elif envelope.channel == CHANNEL_DEBUG:
            if envelope.sequence != self.expected_debug_output:
                raise RuntimeError(
                    f"debug output sequence gap: expected {self.expected_debug_output}, "
                    f"received {envelope.sequence}"
                )
            self.expected_debug_output += 1
            value = message_value(envelope.payload, envelope.payload_tag)
            self._handle_debug(envelope.payload_tag, value, envelope.correlation_id)
        else:
            raise RuntimeError(f"unknown output channel {envelope.channel}")
        return None

    def _handle_runtime(self, tag: int, value: Any, correlation_id: int | None) -> None:
        if tag == 1:  # ServerHello
            self.session = value[1]
            self.epoch = value[4]
            self.configuration_profile_supported = value.get(7) == 1
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
            self._set_active_wait(self.presentation.input_wait)
            # Decoded envelopes are immutable after dispatch. Presentation events are batched
            # until the poll queue is empty so superseded pending-line updates cross threads
            # only once.
            self._pending_presentation_events = [("snapshot", value)]
            self._wait_event_dirty = True
        elif tag == 41:
            try:
                self.presentation.apply_delta(value)
            except ValueError as error:
                self.events.put(log_event(str(error), LogLevel.WARNING))
                self.send_runtime(94, {0: self.expected_runtime_output - 1})
            else:
                self._set_active_wait(self.presentation.input_wait)
                self._pending_presentation_events.append(("delta", value))
                self._wait_event_dirty = True
        elif tag == 42:  # Effects are intentionally unsupported but must be acknowledged.
            self._acknowledge_effects(value)
        elif tag == 50:
            self._handle_storage(value, correlation_id)
        elif tag == 52:
            self._handle_service(value, correlation_id)
        elif tag == 61:
            self._handle_export_ready(value)
        elif tag == 63:
            self._handle_import_accepted(value)
        elif tag == 66:
            self._handle_import_ready(value)
        elif tag == 68:
            self._handle_export_chunk(value)
        elif tag == 91:
            self._presentation_boundary_dirty = True
            self.events.put(FrontendEvent("shutdown_ready", value))
        elif tag == 92:
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
            rejection = value.get(1, "")
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
            if input_request is not None and not retried_input:
                self.events.put(
                    FrontendEvent("interaction_rejected", copy.deepcopy(input_request.wait))
                )
            cache_export_rejection = correlation_id == self.pending_cache_export_message
            diagnosis_export_rejection = (
                correlation_id == self.pending_export_message and self.pending_export_kind in (3, 4)
            )
            snapshot_export_rejection = (
                correlation_id == self.pending_export_message and self.pending_export_kind == 1
            )
            project_file_export_rejection = (
                correlation_id == self.pending_export_message and self.pending_export_kind == 5
            )
            pending_configuration = getattr(self, "pending_configuration", None)
            configuration_rejection = (
                isinstance(pending_configuration, PendingConfigurationPrepare)
                and correlation_id == pending_configuration.message_id
            ) or (
                isinstance(pending_configuration, PendingConfigurationFinalize)
                and correlation_id == pending_configuration.finalize_message_id
            )
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
            if cache_export_rejection:
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
                if stage == 4 and value.get(0) == 0 and self.pending_diagnosis is not None:
                    self.pending_export_kind = None
                    self.pending_diagnosis.stage = "artifact_wait"
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
            # A presentation may advance after the frontend rendered an observation but
            # before the caller-pumped runtime handles it. This is a benign stale sample;
            # a later rendered revision will submit a replacement observation.
            if (
                not (
                    cache_export_rejection
                    or diagnosis_export_rejection
                    or project_file_export_rejection
                    or configuration_rejection
                )
                and not (projection_request and value.get(0) == 2 and stale_projection)
                and not retried_input
            ):
                code = enum_text(value.get(0), COMMAND_ERROR_CODES, "CommandErrorCode")
                self.events.put(FrontendEvent("runtime_error", f"命令被拒绝 [{code}]：{rejection}"))
        elif tag == 96:
            self.epoch = value[0]
            self.phase = value[1]
            self.presentation.apply_snapshot(value[3])
            self._set_active_wait(self.presentation.input_wait)
            self._pending_presentation_events = [("snapshot", value[3])]
            self._wait_event_dirty = True
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

    def _handle_project_report(self, report: dict[int, Any]) -> None:
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
            self.events.put(FrontendEvent("status", "项目文件缓存未命中，正在读取项目源码…"))
            self.pending_bundle = self.pending_bundle.materialize(self._project_scan_progress)
            self._submit_project(None)
            return
        if not report[1]:
            self.fail_startup("project load failed")
            self.reload_candidate = None
            self.events.put(FrontendEvent("runtime_error", "项目加载或热重载失败，请查看日志。"))
            return
        if self.reload_candidate is not None and report[0] == self.reload_candidate.revision:
            self.bundle = self.reload_candidate
            self.reload_candidate = None
            self.storage = self._storage_for_bundle(self.bundle)
            self.cache_refresh_pending = True
            self.cache_preparation_started = False
            self.cache_refresh_after_ns = time.monotonic_ns() + COMPILED_CACHE_PERSIST_DELAY_NS
            self.events.put(FrontendEvent("status", "脚本热重载完成。"))
            self._publish_configuration(report.get(4))
            return
        if self.pending_bundle is not None:
            self.bundle = self.pending_bundle
            self.pending_bundle = None
            self.storage = self._storage_for_bundle(self.bundle)
        self.events.put(FrontendEvent("project_loaded", self.bundle.root if self.bundle else None))
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
        self._continue_project_start(cache_hit)

    def _continue_project_start(self, cache_hit: bool) -> None:
        if self.pending_restore is not None:
            _path, payload, purpose = self.pending_restore
            kind = 0 if purpose == "traditional_save" else 1
            self._begin_import(payload, kind, purpose)
            return
        if cache_hit:
            self.cache_refresh_pending = False
            self.cache_ready = False
            self.cache_preparation_started = False
            self.events.put(FrontendEvent("status", "项目文件缓存命中，正在进入标题画面…"))
            self._submit_start(self._new_game_start())
        else:
            self.cache_refresh_pending = True
            self.cache_preparation_started = False
            self.cache_refresh_after_ns = time.monotonic_ns() + COMPILED_CACHE_PERSIST_DELAY_NS
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
                self._continue_project_start(cache_hit)
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
        self.events.put(FrontendEvent("status", "正在提交项目并编译脚本…"))
        request: dict[int, Any] = {0: self.pending_bundle.identity()}
        if self.pending_bundle.is_materialized:
            request[1] = self.pending_bundle.manifest()
        if cache_transfer_id is not None:
            request[2] = cache_transfer_id
        self.send_runtime(19, request)

    def _begin_import(self, payload: bytes, kind: int, purpose: str) -> None:
        self.import_bytes = payload
        self.import_purpose = purpose
        self.send_runtime(
            62,
            {0: kind, 1: len(payload), 2: blake3.blake3(payload).digest()},
        )

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
        cache_path = (
            self.storage.compiled_cache_path()
            if self.storage and self.allow_compiled_cache_load
            else None
        )
        if cache_path is not None:
            try:
                if cache_path.stat().st_size > 0:
                    self.events.put(FrontendEvent("status", "正在载入项目文件缓存…"))
                    self._stage_project_cache_file(cache_path, "project_cache")
                    return
            except OSError as error:
                self.events.put(log_event(f"读取项目文件缓存失败：{error}", LogLevel.WARNING))
        if self.pending_bundle is None:
            return
        self.pending_bundle = self.pending_bundle.materialize(self._project_scan_progress)
        self._submit_project(None)

    def _refresh_compiled_cache(self, after: str) -> None:
        self.cache_refresh_pending = False
        self.cache_ready = False
        self.pending_cache_after = after
        self.pending_export_kind = 2
        if self.storage is None:
            self._finish_cache_export(False)
            return
        cache_path = self.storage.compiled_cache_path()
        if self.pending_cache_stream is None:
            self.pending_cache_stream = AtomicExportStream.open(cache_path)
        self.pending_export = (cache_path, bytearray(), None)
        if not self.cache_preparation_started:
            self.cache_preparation_started = True
            self.events.put(FrontendEvent("status", "正在后台生成项目文件…"))
        self.pending_cache_export_message = self.send_runtime(60, {0: 2, 1: 0})

    def maybe_refresh_compiled_cache(self) -> None:
        if (
            self.full_project_export is not None
            and self.pending_export is None
            and time.monotonic_ns() >= self.full_project_export.retry_after_ns
        ):
            self._request_project_file_export()
            return
        if self.pending_diagnosis is not None:
            if self.pending_diagnosis.stage == "export_wait" and self.pending_export is None:
                self._start_diagnosis_snapshot_export()
            elif (
                self.pending_diagnosis.stage == "artifact_wait"
                and self.pending_export is None
                and time.monotonic_ns() >= self.pending_diagnosis.retry_after_ns
            ):
                self._start_diagnosis_artifact_export()
            return
        if (
            self.cache_refresh_pending
            and self.pending_export is None
            and time.monotonic_ns() >= self.cache_refresh_after_ns
        ):
            self._refresh_compiled_cache("background")

    def defer_compiled_cache_refresh(self) -> None:
        """Keep cache compression out of latency-sensitive gameplay transitions."""

        if self.cache_refresh_pending:
            self.cache_refresh_after_ns = max(
                self.cache_refresh_after_ns,
                time.monotonic_ns() + COMPILED_CACHE_PERSIST_DELAY_NS,
            )

    def _handle_wait_change(self, change: list[Any]) -> None:
        tag, fields = unwrap_variant(change)
        if tag in (0, 1):
            self._set_active_wait(fields[0])
        elif tag == 2 and self.active_wait and self.active_wait.get(0) == fields[0]:
            self._set_active_wait(None)
        self._wait_event_dirty = True

    def _set_active_wait(self, wait: dict[int, Any] | None) -> None:
        self.active_wait = wait

    def _acknowledge_effects(self, batch: dict[int, Any]) -> None:
        # The TUI cannot play device effects. Reporting Unsupported is semantically different
        # from silently claiming playback succeeded and lets scripts observe honest outcomes.
        outcomes = []
        for effect in batch.get(0, []):
            kind, _fields = unwrap_variant(effect[1])
            if kind == 4:
                self.events.put(FrontendEvent("open_configuration"))
                outcomes.append({0: effect[0], 1: 0})
            else:
                outcomes.append({0: effect[0], 1: 1, 2: "TUI does not provide this device effect"})
        self.send_runtime(43, {0: outcomes})

    def _handle_storage(self, request: dict[int, Any], correlation_id: int | None) -> None:
        if self.storage is None:
            result = {0: request[0], 1: variant(4, {0: 6, 1: "no active project storage"})}
        else:
            result = self.storage.handle(request)
        self.send_runtime(51, result, correlation_id=correlation_id)

    def _handle_service(self, request: dict[int, Any], correlation_id: int | None) -> None:
        request_id, kind, operation = request[0], request[1], request[2]
        try:
            if kind == 9 and operation == "random_seed":
                response = {0: secrets.randbits(64)}
            elif kind == 8 and operation == "local_date_time":
                now = datetime.now().astimezone()
                offset = now.utcoffset()
                response = {
                    0: now.year,
                    1: now.month,
                    2: now.day,
                    3: now.hour,
                    4: now.minute,
                    5: now.second,
                    6: now.microsecond // 1000,
                    7: int(offset.total_seconds() // 60) if offset else 0,
                }
            elif kind == 7 and operation == "get_key_state":
                response = {0: True, 1: False, 2: False}
            elif kind == 1 and operation == "image_metadata":
                query = decode(request[4])
                bundle = next(
                    (
                        candidate
                        for candidate in (self.reload_candidate, self.pending_bundle, self.bundle)
                        if candidate is not None
                    ),
                    None,
                )
                if bundle is None:
                    raise RuntimeError("image metadata requested without a project")
                response = decode_image_metadata(bundle.resource_bytes(query[0], query[1]))
            elif kind == 10 and operation == "get_display_line":
                query = decode(request[4])
                index = query[1]
                text = ""
                if 0 <= index < len(self.presentation.lines):
                    text = plain_line(self.presentation.lines[index])
                response = {0: query[0], 1: text}
            elif kind == 10 and operation == "html_get_printed_str":
                query = decode(request[4])
                response = {
                    0: query[0],
                    1: html_printed_str(self.presentation.lines, query[1]),
                }
            elif kind == 10 and operation == "serialize_physical_history":
                query = decode(request[4])
                body = "\n".join(plain_line(line) for line in self.presentation.lines)
                response = {0: query[0], 1: body if query[2] else f"{query[1]}\n\n{body}"}
            elif kind == 0 and operation == "gget_text_size":
                query = decode(request[4])
                response = {0: query[0], 1: cell_len(query[1]), 2: 1}
            else:
                service = enum_text(kind, SERVICE_KINDS, "ServiceKind")
                raise NotImplementedError(f"unsupported frontend service {service}/{operation}")
            result = variant(0, encode(response))
        except Exception as error:  # noqa: BLE001 - external-service boundary
            result = variant(1, {0: "frontend.unsupported_service", 1: str(error)})
        self.send_runtime(53, {0: request_id, 1: result}, correlation_id=correlation_id)

    def _advance_deadline(self) -> None:
        if not self.active_wait or self.active_wait.get(8) is None:
            return
        now = time.monotonic_ns()
        if now - self.last_time_advance_ns >= 50_000_000:
            self.last_time_advance_ns = now
            self.send_runtime(31, {0: now})

    def submit_text(self, text: str) -> None:
        if self.phase == 7:
            self.events.put(log_event("调试暂停解除前暂不提交游戏输入。", LogLevel.WARNING))
            return
        wait = self.active_wait
        if wait is None:
            self.events.put(log_event("当前没有可提交的输入等待。", LogLevel.WARNING))
            return
        kind = wait[1]
        if is_message_wait(wait):
            intent = message_wait_intent(wait, text or "\n")
        elif kind in (2, 3, 5, 6, 7):
            intent = variant(2, text)
        elif kind == 4:
            intent = variant(4)
        elif kind == 8:
            try:
                values = [int(part.strip()) for part in text.split(",")]
                values += [0] * (5 - len(values))
                primitive = {index: value for index, value in enumerate(values[:5])}
                intent = variant(6, primitive)
            except ValueError:
                self.events.put(
                    FrontendEvent(
                        "error", "原始输入需使用 type,result1,result2,result3,result4 格式。"
                    )
                )
                return
        else:
            intent = variant(2, text)
        self._submit_input(wait, intent)

    def activate(self, button_token: dict[int, int]) -> None:
        if self.phase == 7 or self.active_wait is None:
            return
        self._submit_input(self.active_wait, variant(3, button_token))

    def skip_message_waits(self) -> None:
        wait = self.active_wait
        if not is_message_skip_wait(wait):
            return
        intent = message_wait_intent(wait)
        self._submit_input(wait, intent, message_skip=True)

    def _submit_input(
        self,
        wait: dict[int, Any],
        intent: list[Any],
        *,
        message_skip: bool = False,
        stale_retries: int = 0,
    ) -> None:
        message_id = self.send_runtime(
            30,
            {
                0: wait[0],
                1: wait[11],
                2: time.monotonic_ns(),
                3: intent,
                4: message_skip,
            },
        )
        if len(self._input_messages) >= 256:
            self._input_messages.pop(next(iter(self._input_messages)))
        self._input_messages[message_id] = PendingGameInput(
            copy.deepcopy(wait), copy.deepcopy(intent), message_skip, stale_retries
        )
        if self.single_step_enabled and self.stop_token is None:
            self.request_debug_action("pause_only")

    def _retry_stale_input(self, request: PendingGameInput) -> bool:
        wait = self.active_wait
        if (
            request.stale_retries != 0
            or wait is None
            or wait.get(1) != request.wait.get(1)
            or (wait.get(0), wait.get(11)) == (request.wait.get(0), request.wait.get(11))
        ):
            return False
        self._submit_input(
            wait,
            request.intent,
            message_skip=request.message_skip,
            stale_retries=1,
        )
        return True

    def input_undo(self, token: dict[int, Any] | None) -> None:
        if token is not None:
            self.send_runtime(37, {0: token})

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

    def reload_all(self) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        candidate, request = self.bundle.rescan(self._project_scan_progress)
        self.reload_candidate = candidate
        self.events.put(FrontendEvent("status", f"正在热重载 {len(request[2])} 个文件变更…"))
        self.send_runtime(12, request)

    def reload_file(self, path: Path) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        candidate, request = self.bundle.reload_file(path)
        self.reload_candidate = candidate
        self.events.put(FrontendEvent("status", f"正在热重载 {path.name}…"))
        self.send_runtime(12, request)

    def export_snapshot(self, path: Path, purpose: str) -> None:
        purpose_id = {"normal": 0, "debug": 1}.get(purpose)
        if purpose_id is None:
            raise ValueError(f"unknown snapshot export purpose {purpose}")
        self.pending_export = (path, bytearray(), None)
        self.pending_export_kind = 1
        self.pending_export_message = self.send_runtime(60, {0: 1, 1: purpose_id})

    def export_project_file(self, path: Path, cancelled: Callable[[], bool] | None = None) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        if self.pending_export_kind == 2 or self.cache_preparation_started:
            self.send_runtime(71, {0: 2})
            self.pending_export = None
            self.pending_cache_export_message = None
            self.pending_export_kind = None
            if self.pending_cache_stream is not None:
                self.pending_cache_stream.cancel()
                self.pending_cache_stream = None
            self.cache_preparation_started = False
            self.cache_refresh_pending = True
            self.cache_refresh_after_ns = time.monotonic_ns() + COMPILED_CACHE_RETRY_NS
        if self.bundle.project_file is None:
            full_bundle = self.bundle.materialize(self._project_scan_progress, cancelled)
            self.send_runtime(70, {0: full_bundle.manifest()})
        self.full_project_export = FullProjectExport(path, AtomicExportStream.open(path))
        self._request_project_file_export()

    def _request_project_file_export(self) -> None:
        export = self.full_project_export
        if export is None:
            return
        self.pending_export = (export.target, bytearray(), None)
        self.pending_export_kind = 5
        self.pending_export_message = self.send_runtime(60, {0: 3, 1: 0})

    def cancel_project_file_export(self) -> None:
        if self.pending_export_kind != 5 and self.full_project_export is None:
            return
        self.send_runtime(71, {0: 3})
        self._finish_project_file_export(None, "已取消导出全量项目文件")

    def export_diagnosis(self, path: Path, logs: str, project_name: str) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        if self.pending_diagnosis is not None:
            raise RuntimeError("another diagnosis export is already active")
        self.pending_diagnosis = DiagnosisExport(
            target=path,
            project_name=diagnosis_project_name(project_name),
            logs=logs,
            stage="export_wait",
        )
        if self.pending_export is None:
            self._start_diagnosis_snapshot_export()

    def _start_diagnosis_snapshot_export(self) -> None:
        diagnosis = self.pending_diagnosis
        if diagnosis is None:
            return
        diagnosis.stage = "snapshot"
        self.pending_export = (diagnosis.target, bytearray(), None)
        self.pending_export_kind = 3
        self.pending_export_message = self.send_runtime(60, {0: 1, 1: 2})

    def _handle_export_ready(self, ready: dict[int, Any]) -> None:
        if self.pending_export is None:
            return
        self.pending_export_message = None
        result_tag, fields = unwrap_variant(ready[1])
        if result_tag == 1:
            reasons = enum_list_text(
                fields[0], SNAPSHOT_INELIGIBLE_REASONS, "SnapshotIneligibleReason"
            )
            self.events.put(FrontendEvent("runtime_error", f"当前状态不能生成快照：{reasons}"))
            self.pending_export = None
            self.pending_export_message = None
            if self.pending_export_kind == 2:
                self._finish_cache_export(False)
            elif self.pending_export_kind == 5:
                self._finish_project_file_export(False)
            elif self.pending_export_kind in (3, 4):
                self._finish_diagnosis_export(False, f"当前状态不能导出：{reasons}")
            else:
                self.pending_export_kind = None
                self.events.put(FrontendEvent("snapshot_export_finished", False))
            return
        descriptor = fields[0]
        path, data, _ = self.pending_export
        self.pending_export = (path, data, descriptor)
        self.send_runtime(67, {0: descriptor[0], 1: 0, 2: 1024 * 1024})

    def _handle_export_chunk(self, chunk: dict[int, Any]) -> None:
        if self.pending_export is None:
            return
        path, data, descriptor = self.pending_export
        stream = (
            self.full_project_export.stream
            if self.pending_export_kind == 5 and self.full_project_export is not None
            else self.pending_cache_stream
            if self.pending_export_kind == 2
            else None
        )
        received = stream.received if stream is not None else len(data)
        if descriptor is None or chunk[0] != descriptor[0] or chunk[1] != received:
            raise RuntimeError("snapshot export chunk is out of sequence")
        if self.pending_export_kind in (2, 5):
            if stream is None:
                raise RuntimeError("export stream is missing")
            stream.write(bytes(chunk[2]))
        else:
            data.extend(chunk[2])
        if not chunk[3]:
            self.send_runtime(67, {0: descriptor[0], 1: received + len(chunk[2]), 2: 1024 * 1024})
            return
        if self.pending_export_kind not in (2, 5) and (
            len(data) != descriptor[2] or blake3.blake3(data).digest() != descriptor[3]
        ):
            raise RuntimeError("snapshot export digest verification failed")
        export_kind = self.pending_export_kind
        self.pending_export = None
        self.pending_export_message = None
        if export_kind == 2:
            if self.pending_cache_stream is None:
                raise RuntimeError("cache export stream is missing")
            self.pending_cache_stream.finish(descriptor[2], descriptor[3])
            self.pending_cache_stream = None
            self._finish_cache_export(True)
        elif export_kind == 5:
            if self.full_project_export is None:
                raise RuntimeError("project export stream is missing")
            self.full_project_export.stream.finish(descriptor[2], descriptor[3])
            self._finish_project_file_export(True, f"项目文件已导出到 {path}")
        elif export_kind == 3:
            if self.pending_diagnosis is None:
                raise RuntimeError("diagnosis export state is missing")
            self.pending_diagnosis.snapshot = bytes(data)
            self._start_diagnosis_artifact_export()
        elif export_kind == 4:
            if self.pending_diagnosis is None:
                raise RuntimeError("diagnosis export state is missing")
            self.pending_diagnosis.compiled_artifact = bytes(data)
            try:
                write_diagnosis_archive(
                    self.pending_diagnosis.target,
                    project_name=self.pending_diagnosis.project_name,
                    snapshot=self.pending_diagnosis.snapshot or b"",
                    logs=self.pending_diagnosis.logs,
                    compiled_artifact=self.pending_diagnosis.compiled_artifact,
                )
            except Exception as error:  # noqa: BLE001 - report filesystem/compression failures
                self._finish_diagnosis_export(False, str(error))
            else:
                self._finish_diagnosis_export(True, str(self.pending_diagnosis.target))
        else:
            _atomic_write(path, data)
            self.pending_export_kind = None
            self.events.put(FrontendEvent("snapshot_export_finished", True))
            self.events.put(FrontendEvent("status", f"VM 快照已导出到 {path}"))

    def _start_diagnosis_artifact_export(self) -> None:
        diagnosis = self.pending_diagnosis
        if diagnosis is None:
            return
        diagnosis.stage = "artifact"
        self.pending_export = (diagnosis.target, bytearray(), None)
        self.pending_export_kind = 4
        self.pending_export_message = self.send_runtime(60, {0: 2, 1: 0})

    def _finish_diagnosis_export(self, success: bool, message: str) -> None:
        self.pending_export = None
        self.pending_export_kind = None
        self.pending_export_message = None
        self.pending_diagnosis = None
        self.events.put(FrontendEvent("diagnosis_export_finished", (success, message)))

    def _finish_cache_export(self, success: bool) -> None:
        self.pending_export = None
        self.pending_export_message = None
        if not success and self.pending_cache_stream is not None:
            self.pending_cache_stream.cancel()
            self.pending_cache_stream = None
        after = self.pending_cache_after
        self.pending_cache_after = None
        self.pending_export_kind = None
        self.pending_cache_export_message = None
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
                    "项目文件缓存已保存，正在进入标题画面…"
                    if success
                    else "项目文件缓存保存失败，正在进入标题画面…",
                )
            )
            self._submit_start(self._new_game_start())
        elif after == "reload":
            self.events.put(FrontendEvent("status", "脚本热重载完成。"))
        elif after == "background":
            self.events.put(
                FrontendEvent(
                    "status", "项目文件缓存已保存。" if success else "项目文件缓存保存失败。"
                )
            )

    def _finish_project_file_export(self, success: bool | None, status: str | None = None) -> None:
        export = self.full_project_export
        if success is not True and export is not None:
            export.stream.cancel()
        self.full_project_export = None
        self.pending_export = None
        self.pending_export_kind = None
        self.pending_export_message = None
        self.events.put(FrontendEvent("project_file_export_finished", success))
        self.events.put(FrontendEvent("project_progress_finished"))
        if status is not None:
            self.events.put(FrontendEvent("status", status))

    def _handle_import_accepted(self, accepted: dict[int, Any]) -> None:
        if self.import_bytes is None:
            return
        transfer_id = accepted[0]
        self.import_transfer_id = transfer_id
        offset = 0
        while offset < len(self.import_bytes):
            part = self.import_bytes[offset : offset + STATE_IMPORT_CHUNK_BYTES]
            self.send_runtime(64, {0: transfer_id, 1: offset, 2: part})
            offset += len(part)
        self.send_runtime(65, {0: transfer_id})

    def _handle_import_ready(self, ready: dict[int, Any]) -> None:
        if self.import_transfer_id != ready[0]:
            return
        purpose = self.import_purpose
        if purpose in {"project_cache", "project_file"}:
            self._submit_project(ready[0])
        elif purpose == "traditional_save":
            self.events.put(FrontendEvent("status", "传统存档传输完成，正在读档…"))
            self._submit_start({0: variant(1, ready[0])})
        else:
            self.events.put(FrontendEvent("status", "快照传输完成，正在恢复 VM…"))
            self._submit_start({0: variant(2, ready[0])})
        self.import_bytes = None
        self.import_transfer_id = None
        self.import_purpose = None
        self.pending_restore = None

    def restore_snapshot(self, path: Path) -> None:
        payload = path.expanduser().resolve(strict=True).read_bytes()
        self.pending_restore = (path, payload, "snapshot")
        self._begin_import(payload, 1, "snapshot")

    def restore_save(self, path: Path) -> None:
        payload = path.expanduser().resolve(strict=True).read_bytes()
        self.pending_restore = (path, payload, "traditional_save")
        self._begin_import(payload, 0, "traditional_save")

    def enable_debug(self) -> None:
        if self.debug_grant is not None:
            self.events.put(FrontendEvent("debug_enabled", True))
            return
        if not self.debug_requested:
            self.debug_requested = True
            self.send_debug(0, {0: version_range(*DEBUG_VERSION), 1: list(range(10))})

    def disable_debug(self) -> None:
        self.pending_debug_actions.clear()
        self.single_step_enabled = False
        self.transient_pause_owner = None
        self.transient_close_pending = None
        self.debug_disable_pending = True
        if self.debug_step_in_flight:
            return
        if self.stop_token is not None:
            self._debug_request(variant(1, self.stop_token), "disable_continue")
        else:
            self._revoke_debug()

    def _revoke_debug(self) -> None:
        if self.debug_grant is not None:
            self.send_debug(
                2,
                {
                    0: self.debug_grant[1][0],
                    1: "frontend disabled debugging",
                },
            )
        self.debug_requested = False
        self.debug_grant = None
        self.stop_token = None
        self.selected_fiber = None
        self.debug_pending_by_message.clear()
        self.debug_step_in_flight = False
        self.debug_disable_pending = False
        self.events.put(FrontendEvent("debug_enabled", False))

    def set_single_step(self, enabled: bool) -> None:
        self.single_step_enabled = enabled
        if enabled:
            if self.phase == 4 and self.stop_token is None:
                self.request_debug_action("pause_only")
        elif self.stop_token is not None:
            self._debug_request(variant(1, self.stop_token), "continue")

    def request_debug_action(self, action: str, value: Any = None) -> None:
        if self.debug_grant is None:
            self.pending_debug_actions.append((action, value))
            self.enable_debug()
            return
        if self.stop_token is None:
            self.pending_debug_actions.append((action, value))
            owner = _debug_action_owner(action)
            if owner is not None and self.transient_pause_owner is None:
                self.transient_pause_owner = owner
            self._debug_request(variant(0), "pause")
            return
        self._run_debug_action(action, value)

    def close_debug_surface(self, owner: str) -> None:
        if self.transient_pause_owner != owner:
            return
        self.transient_close_pending = owner
        self._resume_transient_pause_if_ready()

    def _resume_transient_pause_if_ready(self) -> None:
        if (
            self.stop_token is None
            or self.transient_pause_owner is None
            or self.transient_close_pending != self.transient_pause_owner
            or any(
                pending not in {"continue", "transient_continue"}
                for pending in self.debug_pending_by_message.values()
            )
        ):
            return
        self._debug_request(variant(1, self.stop_token), "transient_continue")

    def _run_debug_action(self, action: str, value: Any) -> None:
        if self.stop_token is None:
            return
        if action == "variables":
            self._debug_request(variant(10, self.stop_token, None, 500), "variables")
        elif action == "read_variable":
            descriptor, indices = value if isinstance(value, tuple) else (value, None)
            storage = descriptor[2]
            if storage == 3:
                self.events.put(
                    FrontendEvent("error", "局部变量读取需要先在栈查看器中选择具体 frame。")
                )
                return
            reference = {
                0: descriptor[0],
                1: storage,
                4: self.stop_token[2],
                6: list(indices) if indices is not None else [0 for _ in descriptor.get(4, [])],
            }
            if storage == 2:
                reference[5] = 0
            self._debug_request(variant(11, self.stop_token, reference), "variable_value")
        elif action == "fibers":
            self._debug_request(variant(30, self.stop_token, value, 1024), "fibers")
        elif action == "call_stack":
            self._debug_request(variant(31, self.stop_token, int(value)), "call_stack")
        elif action == "console_evaluate":
            self._debug_request(variant(40, self.stop_token, variant(0, str(value))), "console")
        elif action == "console_execute":
            self._debug_request(variant(40, self.stop_token, variant(1, str(value))), "console")
        elif action == "pause_only":
            return

    def _debug_request(self, command: list[Any], pending: str) -> None:
        if self.debug_grant is None:
            return
        self.send_debug(10, {0: self.debug_grant[1], 1: command}, pending=pending)

    def debug_step(self) -> None:
        if self.stop_token is not None and self.selected_fiber is not None:
            self.debug_step_in_flight = True
            self._debug_request(variant(2, self.stop_token, self.selected_fiber or 0, 1), "step")

    def _handle_debug(self, tag: int, value: Any, correlation_id: int | None) -> None:
        if tag == 1:
            self.debug_grant = value
            self.events.put(FrontendEvent("debug_enabled", True))
            pending = list(self.pending_debug_actions)
            self.pending_debug_actions.clear()
            for action in pending:
                self.request_debug_action(*action)
        elif tag == 2:
            self.debug_grant = None
            self.stop_token = None
            self.selected_fiber = None
            self.debug_step_in_flight = False
            self.debug_disable_pending = False
            self.transient_pause_owner = None
            self.transient_close_pending = None
            self.events.put(FrontendEvent("debug_enabled", False))
        elif tag == 11:
            response_tag, fields = unwrap_variant(value)
            pending = self.debug_pending_by_message.pop(correlation_id or 0, "")
            if response_tag == 8 and fields:
                self.stop_token = fields[0].get(0, self.stop_token)
            self.events.put(FrontendEvent("debug_response", (pending, response_tag, fields)))
            if pending in {
                "continue",
                "disable_continue",
                "transient_continue",
                "auto_continue",
                "step",
            }:
                self.stop_token = None
            if pending == "disable_continue":
                self._revoke_debug()
                return
            if pending == "transient_continue":
                self.transient_pause_owner = None
                self.transient_close_pending = None
            self._resume_transient_pause_if_ready()
        elif tag == 12:
            self.stop_token = value[0]
            self.selected_fiber = value.get(2)
            self.debug_step_in_flight = False
            if self.debug_disable_pending:
                self._debug_request(variant(1, self.stop_token), "disable_continue")
                return
            self._presentation_boundary_dirty = True
            self.events.put(FrontendEvent("debug_stopped", value))
            pending = list(self.pending_debug_actions)
            self.pending_debug_actions.clear()
            for action, argument in pending:
                self._run_debug_action(action, argument)
            if self.single_step_enabled and unwrap_variant(value[1])[0] == 3:
                self._debug_request(variant(1, self.stop_token), "auto_continue")
            self._resume_transient_pause_if_ready()
        elif tag == 13:
            pending = self.debug_pending_by_message.pop(correlation_id or 0, "")
            if pending == "step":
                self.debug_step_in_flight = False
            if self.debug_disable_pending:
                self._revoke_debug()
            self.events.put(FrontendEvent("runtime_error", f"调试请求失败：{value.get(1, '')}"))

    def shutdown(self) -> None:
        if not self.shutting_down and self.session is not None:
            self.shutting_down = True
            self.send_runtime(90, {0: True})


def _atomic_write(path: Path, data: bytes | bytearray) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
