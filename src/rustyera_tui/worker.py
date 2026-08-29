"""Threaded C ABI worker for the Textual frontend."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .abi import RuntimeAbi
from .log_model import LogLevel, LogMessage
from .project import ProjectBundle
from .runtime_export import ExportStage
from .runtime_types import FrontendCommand, FrontendEvent
from .startup_telemetry import emit_startup_milestone
from .worker_project import _WorkerProjectMixin

MAX_WORKER_COMMANDS = 256
MAX_WORKER_EVENTS = 64

if TYPE_CHECKING:
    from .runtime import RuntimeClient


class _NotifyingEventQueue(queue.Queue[FrontendEvent]):
    """Bounded worker queue that nudges Textual after a successful publish."""

    def __init__(self, maxsize: int, notify: Callable[[], None]) -> None:
        super().__init__(maxsize)
        self._notify = notify

    def put(
        self,
        item: FrontendEvent,
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        super().put(item, block, timeout)
        self._notify()


class RuntimeWorker(_WorkerProjectMixin, threading.Thread):
    """Serialize all C ABI calls while exposing queue-only communication to Textual."""

    def __init__(
        self,
        runtime_library: Path | None,
        initial_project: Path | None,
        *,
        new_game_seed: int | None = None,
        metrics_threshold_ms: float | None = None,
        initial_state: tuple[Path, str] | None = None,
        initial_project_file: Path | None = None,
    ):
        super().__init__(name="rustyera-runtime", daemon=False)
        self.runtime_library = runtime_library
        self.initial_project = initial_project
        self.new_game_seed = new_game_seed
        self.metrics_threshold_ms = metrics_threshold_ms
        self.initial_state = initial_state
        self.initial_project_file = initial_project_file
        self.commands: queue.Queue[FrontendCommand] = queue.Queue(maxsize=MAX_WORKER_COMMANDS)
        self._projection_command_lock = threading.Lock()
        self._pending_projection: Any = None
        self._projection_command_queued = False
        # Backpressure is preferable to retaining an unbounded number of presentation
        # revisions when Runtime can produce output faster than Textual lays it out.
        self._event_notifier_lock = threading.Lock()
        self._event_notifier: Callable[[], None] | None = None
        self.events: queue.Queue[FrontendEvent] = _NotifyingEventQueue(
            MAX_WORKER_EVENTS, self._notify_event_available
        )
        self._stop_requested = threading.Event()
        self._project_export_cancelled = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self.client: RuntimeClient | None = None

    def set_event_notifier(self, notifier: Callable[[], None] | None) -> None:
        """Install the UI-thread bridge used to drain newly published events promptly."""

        with self._event_notifier_lock:
            self._event_notifier = notifier

    def _notify_event_available(self) -> None:
        with self._event_notifier_lock:
            notifier = self._event_notifier
        if notifier is not None:
            try:
                notifier()
            except Exception:  # noqa: BLE001 - notification is advisory; polling remains active
                pass

    def send(self, kind: str, value: Any = None) -> None:
        if kind == "cancel_project_file_export":
            self._project_export_cancelled.set()
        if kind == "projection":
            with self._projection_command_lock:
                self._pending_projection = value
                if self._projection_command_queued:
                    return
                self._projection_command_queued = True
            try:
                self.commands.put_nowait(FrontendCommand(kind))
            except queue.Full:
                with self._projection_command_lock:
                    self._projection_command_queued = False
                return
            return
        try:
            self.commands.put_nowait(FrontendCommand(kind, value))
        except queue.Full as error:
            raise RuntimeError("runtime command queue is saturated") from error

    def run(self) -> None:
        from .runtime import RuntimeClient

        abi: RuntimeAbi | None = None
        try:
            abi = RuntimeAbi(
                self.runtime_library,
                resource_directory=self.initial_project,
                project_progress=self._emit_project_progress,
            )
            self.client = RuntimeClient(
                abi,
                self.events,
                new_game_seed=self.new_game_seed,
                metrics_threshold_ms=self.metrics_threshold_ms,
            )
            if self.initial_project is not None:
                self._load_project(self.initial_project)
            elif self.initial_project_file is not None:
                self._load_project_file(self.initial_project_file)
            else:
                self.events.put(FrontendEvent("status", "请选择 Era 项目文件夹。"))
            while not self._stop_requested.is_set():
                self._process_commands()
                if not self.client.can_pump:
                    try:
                        command = self.commands.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    self._process_command(command)
                    continue
                busy = self.client.pump()
                self.client.maybe_refresh_compiled_cache()
                if not busy:
                    try:
                        command = self.commands.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    self._process_command(command)
        except InterruptedError as error:
            if not self._stop_requested.is_set():
                emit_startup_milestone("failed", attempt_id=0, scenario="unknown", error=str(error))
                self._emit_terminal_event(
                    FrontendEvent("error", f"前端 Runtime worker 失败：{error}")
                )
        except Exception as error:  # noqa: BLE001 - worker must report all boundary failures
            if self.client is not None:
                if self.client.full_project_export is not None:
                    self.client._finish_project_file_export(False)
                if (
                    self.client.pending_export is not None
                    and self.client.pending_export.stage == ExportStage.COMPILED_CACHE
                ):
                    self.client._finish_cache_export(False)
                if self.client.pending_diagnosis is not None:
                    self.client._finish_diagnosis_export(False, str(error))
                self.client.fail_startup(error)
            else:
                emit_startup_milestone("failed", attempt_id=0, scenario="unknown", error=str(error))
            self._emit_terminal_event(FrontendEvent("error", f"前端 Runtime worker 失败：{error}"))
        finally:
            if abi is not None:
                try:
                    abi.close()
                except Exception as error:  # noqa: BLE001
                    self._emit_terminal_event(
                        FrontendEvent(
                            "log",
                            LogMessage(LogLevel.WARNING, f"关闭 Runtime session 失败：{error}"),
                        )
                    )
            if self.client is not None:
                try:
                    self.client._reset_wire_state()
                except Exception as error:  # noqa: BLE001 - shutdown remains best effort
                    self._emit_terminal_event(
                        FrontendEvent(
                            "log",
                            LogMessage(LogLevel.WARNING, f"释放前端 Runtime 状态失败：{error}"),
                        )
                    )
                self.client = None
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break
            self._emit_terminal_event(FrontendEvent("worker_stopped"))

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
        wait_bound_input = command.kind in {
            "submit_text",
            "skip_message_waits",
            "activate",
            "input_undo",
        }
        submitted_wait = client.active_wait if wait_bound_input else None
        try:
            if command.kind in {"submit_text", "skip_message_waits", "activate", "input_undo"}:
                client.defer_compiled_cache_refresh()
            match command.kind:
                case "load_project":
                    self._load_project(Path(command.value))
                case "load_project_file":
                    self._load_project_file(Path(command.value))
                case "restart":
                    if client.bundle is None:
                        raise RuntimeError("no project is active")
                    if client.bundle.project_file is not None:
                        self._load_project_file(client.bundle.project_file)
                    else:
                        self._load_project(client.bundle.root)
                case "restart_recompile":
                    if client.bundle is None:
                        raise RuntimeError("no project is active")
                    if client.bundle.project_file is not None:
                        raise RuntimeError(
                            "a packaged project cannot be recompiled without sources"
                        )
                    root = client.bundle.root
                    client.begin_startup_attempt(project_file=False)
                    client.begin_session_reset()
                    client.recreate(
                        ProjectBundle.scan(root, 1, client._project_scan_progress),
                        allow_compiled_cache=False,
                    )
                case "return_title":
                    if client.bundle is None:
                        raise RuntimeError("no project is active")
                    message_id = client.send_runtime(23, {})
                    client.begin_game_state_transition(message_id)
                case "save_configuration":
                    changes, restart = command.value
                    client.prepare_configuration_update(changes, restart)
                case "save_client_preferences":
                    scope, values = command.value
                    client.save_client_preferences(str(scope), values)
                case "reload_all":
                    client.reload_all()
                case "reload_folder":
                    client.reload_folder(Path(command.value))
                case "reload_file":
                    client.reload_file(Path(command.value))
                case "submit_text":
                    client.submit_text(str(command.value))
                case "skip_message_waits":
                    client.skip_message_waits()
                case "activate":
                    client.activate(command.value)
                case "input_undo":
                    client.input_undo(command.value)
                case "device_pump_ack":
                    client.complete_device_pump(int(command.value))
                case "projection":
                    with self._projection_command_lock:
                        projection = self._pending_projection
                        self._pending_projection = None
                        self._projection_command_queued = False
                    if projection is not None:
                        client.projection(*projection)
                case "export_snapshot":
                    path, purpose = command.value
                    client.export_snapshot(Path(path), str(purpose))
                case "export_input_replay":
                    client.export_input_replay(Path(command.value))
                case "export_project_file":
                    self._project_export_cancelled.clear()
                    client.export_project_file(
                        Path(command.value), self._project_export_cancelled.is_set
                    )
                case "cancel_project_file_export":
                    client.cancel_project_file_export()
                case "export_diagnosis":
                    path, logs, project_name = command.value
                    client.export_diagnosis(Path(path), str(logs), str(project_name))
                case "restore_snapshot":
                    if client.bundle is None:
                        raise RuntimeError("load the matching project before restoring a snapshot")
                    path = Path(command.value).expanduser().resolve(strict=True)
                    client.restore_snapshot(path)
                case "restore_save":
                    if client.bundle is None:
                        raise RuntimeError("load the matching project before restoring a save")
                    path = Path(command.value).expanduser().resolve(strict=True)
                    client.restore_save(path)
                case "debug_enable":
                    client.enable_debug()
                case "debug_disable":
                    client.disable_debug()
                case "debug_single_step":
                    client.set_single_step(bool(command.value))
                case "debug_action":
                    action, value = command.value
                    client.request_debug_action(action, value)
                case "debug_surface_closed":
                    client.close_debug_surface(str(command.value))
                case "debug_step":
                    client.debug_step()
                case "shutdown":
                    client.shutdown()
                case "force_stop":
                    self._stop_requested.set()
                case _:
                    raise ValueError(f"unknown frontend command {command.kind}")
        except Exception as error:  # noqa: BLE001 - command boundary
            if command.kind == "export_project_file" and self._project_export_cancelled.is_set():
                client._finish_project_file_export(None, "已取消导出全量项目文件")
                return
            client.fail_startup(error)
            if wait_bound_input and submitted_wait is not None:
                self.events.put(FrontendEvent("interaction_rejected", submitted_wait))
            if command.kind in {"export_snapshot", "export_input_replay"}:
                if client.pending_export is not None:
                    client.pending_export.cancel()
                client.pending_export = None
                event = (
                    "input_replay_export_finished"
                    if command.kind == "export_input_replay"
                    else "snapshot_export_finished"
                )
                self.events.put(FrontendEvent(event, False))
            elif command.kind == "export_project_file":
                client._finish_project_file_export(False)
            elif command.kind == "export_diagnosis":
                client._finish_diagnosis_export(False, str(error))
            self.events.put(FrontendEvent("error", str(error)))

    def stop(self) -> None:
        self._stop_requested.set()
        # Once the UI is exiting, presentation events are no longer useful. Freeing the
        # bounded queue also releases a producer blocked while publishing its final batch.
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break
        with self._projection_command_lock:
            self._pending_projection = None
            self._projection_command_queued = False

    def shutdown(self) -> None:
        """Stop and join exactly once so the C ABI session is closed before returning."""

        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self.stop()
            if self.ident is not None and threading.current_thread() is not self:
                self.join()
            self._shutdown_complete = not self.is_alive()

    def _emit_terminal_event(self, event: FrontendEvent) -> None:
        """Publish shutdown/error state even if the presentation queue was saturated."""

        while True:
            try:
                self.events.put_nowait(event)
                return
            except queue.Full:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    continue
