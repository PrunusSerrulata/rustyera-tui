"""Caller-pumped runtime client and its frontend worker thread."""

from __future__ import annotations

import copy
import os
import queue
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import blake3
from rich.cells import cell_len

from .abi import RuntimeAbi
from .presentation import PresentationModel
from .project import ProjectBundle, StorageBackend
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


@dataclass(frozen=True, slots=True)
class FrontendEvent:
    kind: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class FrontendCommand:
    kind: str
    value: Any = None


class RuntimeClient:
    """Translate frontend intent to the public runtime and debug wire protocols."""

    def __init__(
        self,
        abi: RuntimeAbi,
        events: queue.Queue[FrontendEvent],
    ) -> None:
        self.abi = abi
        self.events = events
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
        self.presentation = PresentationModel()
        self.active_wait: dict[int, Any] | None = None
        self.pending_restore: tuple[Path, bytes] | None = None
        self.pending_export: tuple[Path, bytearray, dict[int, Any] | None] | None = None
        self.import_bytes: bytes | None = None
        self.import_transfer_id: int | None = None
        self.debug_requested = False
        self.debug_grant: dict[int, Any] | None = None
        self.stop_token: dict[int, Any] | None = None
        self.selected_fiber: int | None = None
        self.pending_debug_actions: list[tuple[str, Any]] = []
        self.debug_pending_by_message: dict[int, str] = {}
        self.last_time_advance_ns = 0
        self.shutting_down = False
        self._send_hello()

    def _reset_wire_state(self) -> None:
        self.runtime_sequence = 0
        self.debug_sequence = 0
        self.next_message_id = 1
        self.session = None
        self.epoch = None
        self.phase = 0
        self.expected_runtime_output = 0
        self.expected_debug_output = 0
        self.presentation = PresentationModel()
        self.active_wait = None
        self.debug_requested = False
        self.debug_grant = None
        self.stop_token = None
        self.selected_fiber = None
        self.pending_debug_actions.clear()
        self.debug_pending_by_message.clear()
        self.shutting_down = False

    def recreate(self, bundle: ProjectBundle, restore: tuple[Path, bytes] | None = None) -> None:
        self.events.put(FrontendEvent("status", "正在创建新的 Runtime session…"))
        self.abi.recreate_session()
        self._reset_wire_state()
        self.pending_bundle = bundle
        self.pending_restore = restore
        self._send_hello()

    def _send_hello(self) -> None:
        service_capabilities = [
            {0: 9, 1: "random_seed", 2: version_range(1, 0)},
            {0: 8, 1: "local_date_time", 2: version_range(1, 0)},
            {0: 7, 1: "get_key_state", 2: version_range(1, 0)},
            {0: 10, 1: "get_display_line", 2: version_range(1, 0)},
            {0: 10, 1: "serialize_physical_history", 2: version_range(1, 0)},
            {0: 0, 1: "gget_text_size", 2: version_range(1, 0)},
        ]
        capabilities = {
            0: [0, 1],
            1: True,
            2: False,
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
        limits = {
            0: 128 * 1024 * 1024,
            1: 127 * 1024 * 1024,
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
        }
        self.send_runtime(0, hello)

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
        report = self.abi.drive()
        emitted = False
        acknowledge_through: int | None = None
        while data := self.abi.poll():
            emitted = True
            runtime_sequence = self._handle_envelope(data)
            if runtime_sequence is not None:
                acknowledge_through = runtime_sequence
        # Runtime output acknowledgement is cumulative. Deferring it until the complete poll
        # batch also ensures an epoch-changing reload is acknowledged with its final epoch,
        # even when an earlier message in the same batch was emitted before the commit.
        if acknowledge_through is not None and self.session is not None:
            self.send_runtime(93, {0: acknowledge_through})
        self._advance_deadline()
        return emitted or report.state in (1, 2)

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
            self.events.put(
                FrontendEvent("log", f"runtime handshake complete (epoch {self.epoch})")
            )
            if self.pending_bundle is not None:
                self.events.put(FrontendEvent("status", "正在提交项目并编译脚本…"))
                self.send_runtime(10, self.pending_bundle.manifest())
            return
        if tag == 2:
            self.events.put(FrontendEvent("error", f"协议版本被拒绝：{value.get(1, '')}"))
        elif tag == 11:  # ProjectLoadReport
            self._handle_project_report(value)
        elif tag == 21:
            self.phase = value[0]
            self.epoch = value[2]
            self.events.put(FrontendEvent("phase", self.phase))
        elif tag == 22:
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
            self.active_wait = self.presentation.input_wait
            self.events.put(FrontendEvent("presentation_snapshot", copy.deepcopy(value)))
            self.events.put(FrontendEvent("wait", copy.deepcopy(self.active_wait)))
        elif tag == 41:
            try:
                self.presentation.apply_delta(value)
            except ValueError as error:
                self.events.put(FrontendEvent("log", str(error)))
                self.send_runtime(94, {0: self.expected_runtime_output - 1})
            else:
                self.active_wait = self.presentation.input_wait
                self.events.put(FrontendEvent("presentation_delta", copy.deepcopy(value)))
                self.events.put(FrontendEvent("wait", copy.deepcopy(self.active_wait)))
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
            self.events.put(FrontendEvent("shutdown_ready", value))
        elif tag == 92:
            self.events.put(FrontendEvent("error", f"Runtime 故障：{value.get(1, '')}"))
        elif tag == 95:
            self.events.put(FrontendEvent("error", f"命令被拒绝：{value.get(1, '')}"))
        elif tag == 96:
            self.epoch = value[0]
            self.phase = value[1]
            self.presentation.apply_snapshot(value[3])
            self.active_wait = self.presentation.input_wait
            self.events.put(FrontendEvent("presentation_snapshot", copy.deepcopy(value[3])))
            self.events.put(FrontendEvent("wait", copy.deepcopy(self.active_wait)))
        elif tag == 97:
            source = value.get(3)
            location = f" ({source.get(0)}:{source.get(3, '?')})" if source else ""
            self.events.put(FrontendEvent("log", f"{value.get(0)}: {value.get(2)}{location}"))

    def _handle_project_report(self, report: dict[int, Any]) -> None:
        for diagnostic in report.get(2, []):
            source = diagnostic.get(3)
            location = f" {source.get(0)}" if source else ""
            self.events.put(
                FrontendEvent("log", f"{diagnostic.get(0)}:{location} {diagnostic.get(2)}")
            )
        if not report[1]:
            self.reload_candidate = None
            self.events.put(FrontendEvent("error", "项目加载或热重载失败，请查看日志。"))
            return
        if self.reload_candidate is not None and report[0] == self.reload_candidate.revision:
            self.bundle = self.reload_candidate
            self.reload_candidate = None
            self.storage = StorageBackend(self.bundle.root)
            self.events.put(FrontendEvent("status", "脚本热重载完成。"))
            return
        if self.pending_bundle is not None:
            self.bundle = self.pending_bundle
            self.pending_bundle = None
            self.storage = StorageBackend(self.bundle.root)
        self.events.put(FrontendEvent("project_loaded", self.bundle.root if self.bundle else None))
        if self.pending_restore is not None:
            _, payload = self.pending_restore
            self.import_bytes = payload
            self.send_runtime(
                62,
                {
                    0: 1,
                    1: len(payload),
                    2: blake3.blake3(payload).digest(),
                },
            )
        else:
            self.events.put(FrontendEvent("status", "项目已加载，正在进入标题画面…"))
            self.send_runtime(20, {0: variant(0, None)})

    def _handle_wait_change(self, change: list[Any]) -> None:
        tag, fields = unwrap_variant(change)
        if tag in (0, 1):
            self.active_wait = fields[0]
        elif tag == 2 and self.active_wait and self.active_wait.get(0) == fields[0]:
            self.active_wait = None
        self.events.put(FrontendEvent("wait", copy.deepcopy(self.active_wait)))

    def _acknowledge_effects(self, batch: dict[int, Any]) -> None:
        # The TUI cannot play device effects. Reporting Unsupported is semantically different
        # from silently claiming playback succeeded and lets scripts observe honest outcomes.
        outcomes = []
        for effect in batch.get(0, []):
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
            elif kind == 10 and operation == "get_display_line":
                query = decode(request[4])
                index = query[1]
                text = ""
                if 0 <= index < len(self.presentation.lines):
                    text = self._plain_line(self.presentation.lines[index])
                response = {0: query[0], 1: text}
            elif kind == 10 and operation == "serialize_physical_history":
                query = decode(request[4])
                lines = [self._plain_line(line) for line in self.presentation.lines]
                body = "\n".join(lines)
                response = {0: query[0], 1: body if query[2] else f"{query[1]}\n\n{body}"}
            elif kind == 0 and operation == "gget_text_size":
                query = decode(request[4])
                response = {0: query[0], 1: cell_len(query[1]), 2: 1}
            else:
                raise NotImplementedError(f"unsupported frontend service {kind}/{operation}")
            result = variant(0, encode(response))
        except Exception as error:  # noqa: BLE001 - external-service boundary
            result = variant(1, {0: "frontend.unsupported_service", 1: str(error)})
        self.send_runtime(53, {0: request_id, 1: result}, correlation_id=correlation_id)

    @staticmethod
    def _plain_line(line: Any) -> str:
        return "".join(segment.text for segment in line.segments)

    def _advance_deadline(self) -> None:
        if not self.active_wait or self.active_wait.get(8) is None:
            return
        now = time.monotonic_ns()
        if now - self.last_time_advance_ns >= 50_000_000:
            self.last_time_advance_ns = now
            self.send_runtime(31, {0: now})

    def submit_text(self, text: str) -> None:
        if self.phase == 7 and self.stop_token is not None:
            self.debug_step()
            return
        wait = self.active_wait
        if wait is None:
            self.events.put(FrontendEvent("log", "当前没有可提交的输入等待。"))
            return
        kind = wait[1]
        if kind == 0:
            intent = variant(0)
        elif kind == 1:
            intent = variant(1, text or "\n")
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
        if self.active_wait is None:
            return
        self._submit_input(self.active_wait, variant(3, button_token))

    def _submit_input(self, wait: dict[int, Any], intent: list[Any]) -> None:
        self.send_runtime(
            30,
            {
                0: wait[0],
                1: wait[11],
                2: time.monotonic_ns(),
                3: intent,
                4: False,
            },
        )

    def input_undo(self, token: dict[int, Any] | None) -> None:
        if token is not None:
            self.send_runtime(37, {0: token})

    def projection(self, width: int, height: int, environment_revision: int) -> None:
        if self.session is None:
            return
        self.send_runtime(
            35,
            {
                0: environment_revision,
                1: self.presentation.revision,
                2: {0: width, 1: height},
                3: environment_revision,
                4: max(1, width),
                5: "",
                6: {0: 1, 1: 1000, 2: 1, 3: 1000, 4: 0, 5: 0},
            },
        )

    def reload_all(self) -> None:
        if self.bundle is None:
            raise RuntimeError("no project is active")
        candidate, request = self.bundle.rescan()
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

    def export_snapshot(self, path: Path) -> None:
        self.pending_export = (path, bytearray(), None)
        self.send_runtime(60, {0: 1})

    def _handle_export_ready(self, ready: dict[int, Any]) -> None:
        if self.pending_export is None:
            return
        result_tag, fields = unwrap_variant(ready[1])
        if result_tag == 1:
            self.events.put(FrontendEvent("error", f"当前状态不能生成快照：{fields[0]}"))
            self.pending_export = None
            return
        descriptor = fields[0]
        path, data, _ = self.pending_export
        self.pending_export = (path, data, descriptor)
        self.send_runtime(67, {0: descriptor[0], 1: 0, 2: 1024 * 1024})

    def _handle_export_chunk(self, chunk: dict[int, Any]) -> None:
        if self.pending_export is None:
            return
        path, data, descriptor = self.pending_export
        if descriptor is None or chunk[0] != descriptor[0] or chunk[1] != len(data):
            raise RuntimeError("snapshot export chunk is out of sequence")
        data.extend(chunk[2])
        if not chunk[3]:
            self.send_runtime(67, {0: descriptor[0], 1: len(data), 2: 1024 * 1024})
            return
        if len(data) != descriptor[2] or blake3.blake3(data).digest() != descriptor[3]:
            raise RuntimeError("snapshot export digest verification failed")
        _atomic_write(path, bytes(data))
        self.events.put(FrontendEvent("status", f"VM 快照已导出到 {path}"))
        self.pending_export = None

    def _handle_import_accepted(self, accepted: dict[int, Any]) -> None:
        if self.import_bytes is None:
            return
        transfer_id = accepted[0]
        self.import_transfer_id = transfer_id
        offset = 0
        while offset < len(self.import_bytes):
            part = self.import_bytes[offset : offset + 1024 * 1024]
            self.send_runtime(64, {0: transfer_id, 1: offset, 2: part})
            offset += len(part)
        self.send_runtime(65, {0: transfer_id})

    def _handle_import_ready(self, ready: dict[int, Any]) -> None:
        if self.import_transfer_id != ready[0]:
            return
        self.events.put(FrontendEvent("status", "快照传输完成，正在恢复 VM…"))
        self.send_runtime(20, {0: variant(2, ready[0])})
        self.import_bytes = None
        self.import_transfer_id = None
        self.pending_restore = None

    def enable_debug(self) -> None:
        if self.debug_grant is not None:
            self.events.put(FrontendEvent("debug_enabled", True))
            return
        if not self.debug_requested:
            self.debug_requested = True
            self.send_debug(0, {0: version_range(*DEBUG_VERSION), 1: list(range(10))})

    def disable_debug(self) -> None:
        if self.stop_token is not None:
            self._debug_request(variant(1, self.stop_token), "continue")
        self.pending_debug_actions.clear()
        self.events.put(FrontendEvent("debug_enabled", False))

    def request_debug_action(self, action: str, value: Any = None) -> None:
        if self.debug_grant is None:
            self.pending_debug_actions.append((action, value))
            self.enable_debug()
            return
        if self.stop_token is None:
            self.pending_debug_actions.append((action, value))
            self._debug_request(variant(0), "pause")
            return
        self._run_debug_action(action, value)

    def _run_debug_action(self, action: str, value: Any) -> None:
        if self.stop_token is None:
            return
        if action == "variables":
            self._debug_request(variant(10, self.stop_token, None, 500), "variables")
        elif action == "read_variable":
            descriptor = value
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
                6: [0 for _ in descriptor.get(4, [])],
            }
            if storage == 2:
                reference[5] = 0
            self._debug_request(variant(11, self.stop_token, reference), "variable_value")
        elif action == "fibers":
            self._debug_request(variant(30, self.stop_token, None, 100), "fibers")
        elif action == "call_stack":
            self._debug_request(variant(31, self.stop_token, int(value)), "call_stack")
        elif action == "operand_stack":
            fiber_id, frame_id = value
            self._debug_request(
                variant(32, self.stop_token, fiber_id, frame_id, None, 500), "operand_stack"
            )
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
        if self.stop_token is not None:
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
            self.events.put(FrontendEvent("debug_enabled", False))
            self.events.put(FrontendEvent("log", f"调试权限已撤销：{value.get(1, '')}"))
        elif tag == 11:
            response_tag, fields = unwrap_variant(value)
            pending = self.debug_pending_by_message.pop(correlation_id or 0, "")
            self.events.put(FrontendEvent("debug_response", (pending, response_tag, fields)))
            if pending == "continue":
                self.stop_token = None
        elif tag == 12:
            self.stop_token = value[0]
            self.selected_fiber = value.get(2)
            self.events.put(FrontendEvent("debug_stopped", value))
            pending = list(self.pending_debug_actions)
            self.pending_debug_actions.clear()
            for action, argument in pending:
                self._run_debug_action(action, argument)
        elif tag == 13:
            self.events.put(FrontendEvent("error", f"调试请求失败：{value.get(1, '')}"))

    def shutdown(self) -> None:
        if not self.shutting_down and self.session is not None:
            self.shutting_down = True
            self.send_runtime(90, {0: True})


def _atomic_write(path: Path, data: bytes) -> None:
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


class RuntimeWorker(threading.Thread):
    """Serialize all C ABI calls while exposing queue-only communication to Textual."""

    def __init__(self, runtime_library: Path | None, initial_project: Path | None):
        super().__init__(name="rustyera-runtime", daemon=True)
        self.runtime_library = runtime_library
        self.initial_project = initial_project
        self.commands: queue.Queue[FrontendCommand] = queue.Queue()
        self.events: queue.Queue[FrontendEvent] = queue.Queue()
        self._stop_requested = threading.Event()
        self.client: RuntimeClient | None = None

    def send(self, kind: str, value: Any = None) -> None:
        self.commands.put(FrontendCommand(kind, value))

    def run(self) -> None:
        abi: RuntimeAbi | None = None
        try:
            abi = RuntimeAbi(self.runtime_library)
            self.client = RuntimeClient(abi, self.events)
            if self.initial_project is not None:
                self._load_project(self.initial_project)
            else:
                self.events.put(FrontendEvent("status", "请选择 Era 项目文件夹。"))
            while not self._stop_requested.is_set():
                self._process_commands()
                busy = self.client.pump()
                if not busy:
                    try:
                        command = self.commands.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    self._process_command(command)
        except Exception as error:  # noqa: BLE001 - worker must report all boundary failures
            self.events.put(FrontendEvent("error", f"前端 Runtime worker 失败：{error}"))
        finally:
            if abi is not None:
                try:
                    abi.close()
                except Exception as error:  # noqa: BLE001
                    self.events.put(FrontendEvent("log", f"关闭 Runtime session 失败：{error}"))
            self.events.put(FrontendEvent("worker_stopped"))

    def _process_commands(self) -> None:
        for _ in range(64):
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            self._process_command(command)

    def _process_command(self, command: FrontendCommand) -> None:
        client = self.client
        if client is None:
            return
        try:
            match command.kind:
                case "load_project":
                    self._load_project(Path(command.value))
                case "restart" | "return_title":
                    if client.bundle is None:
                        raise RuntimeError("no project is active")
                    client.recreate(ProjectBundle.scan(client.bundle.root, 1))
                case "reload_all":
                    client.reload_all()
                case "reload_file":
                    client.reload_file(Path(command.value))
                case "submit_text":
                    client.submit_text(str(command.value))
                case "activate":
                    client.activate(command.value)
                case "input_undo":
                    client.input_undo(command.value)
                case "projection":
                    client.projection(*command.value)
                case "export_snapshot":
                    client.export_snapshot(Path(command.value))
                case "restore_snapshot":
                    if client.bundle is None:
                        raise RuntimeError("load the matching project before restoring a snapshot")
                    path = Path(command.value).expanduser().resolve(strict=True)
                    client.recreate(
                        ProjectBundle.scan(client.bundle.root, 1), (path, path.read_bytes())
                    )
                case "debug_enable":
                    client.enable_debug()
                case "debug_disable":
                    client.disable_debug()
                case "debug_action":
                    action, value = command.value
                    client.request_debug_action(action, value)
                case "debug_step":
                    client.debug_step()
                case "shutdown":
                    client.shutdown()
                case "force_stop":
                    self._stop_requested.set()
                case _:
                    raise ValueError(f"unknown frontend command {command.kind}")
        except Exception as error:  # noqa: BLE001 - command boundary
            self.events.put(FrontendEvent("error", str(error)))

    def _load_project(self, root: Path) -> None:
        if self.client is None:
            return
        self.events.put(FrontendEvent("status", f"正在扫描 {root}…"))
        bundle = ProjectBundle.scan(root, 1)
        self.client.recreate(bundle)

    def stop(self) -> None:
        self.send("force_stop")
