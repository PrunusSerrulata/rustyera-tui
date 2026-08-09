"""Threaded C ABI worker for the Textual frontend."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .abi import RuntimeAbi
from .log_model import LogLevel, LogMessage
from .project import ProjectBundle
from .runtime_types import FrontendCommand, FrontendEvent
from .startup_telemetry import emit_startup_milestone

if TYPE_CHECKING:
    from .runtime import RuntimeClient


class RuntimeWorker(threading.Thread):
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
        super().__init__(name="rustyera-runtime", daemon=True)
        self.runtime_library = runtime_library
        self.initial_project = initial_project
        self.new_game_seed = new_game_seed
        self.metrics_threshold_ms = metrics_threshold_ms
        self.initial_state = initial_state
        self.initial_project_file = initial_project_file
        self.commands: queue.Queue[FrontendCommand] = queue.Queue()
        # Backpressure is preferable to retaining an unbounded number of presentation
        # revisions when Runtime can produce output faster than Textual lays it out.
        self.events: queue.Queue[FrontendEvent] = queue.Queue(maxsize=4_096)
        self._stop_requested = threading.Event()
        self.client: RuntimeClient | None = None

    def send(self, kind: str, value: Any = None) -> None:
        self.commands.put(FrontendCommand(kind, value))

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
                busy = self.client.pump()
                self.client.maybe_refresh_compiled_cache()
                if not busy:
                    try:
                        command = self.commands.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    self._process_command(command)
        except Exception as error:  # noqa: BLE001 - worker must report all boundary failures
            if self.client is not None:
                self.client.fail_startup(error)
            else:
                emit_startup_milestone("failed", attempt_id=0, scenario="unknown", error=str(error))
            self.events.put(FrontendEvent("error", f"前端 Runtime worker 失败：{error}"))
        finally:
            if abi is not None:
                try:
                    abi.close()
                except Exception as error:  # noqa: BLE001
                    self.events.put(
                        FrontendEvent(
                            "log",
                            LogMessage(LogLevel.WARNING, f"关闭 Runtime session 失败：{error}"),
                        )
                    )
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
                    client.begin_startup_attempt(project_file=False)
                    client.recreate(
                        ProjectBundle.scan(client.bundle.root, 1, client._project_scan_progress),
                        allow_compiled_cache=False,
                    )
                case "return_title":
                    if client.bundle is None:
                        raise RuntimeError("no project is active")
                    client.send_runtime(23, {})
                case "save_configuration":
                    changes, restart = command.value
                    client.prepare_configuration_update(changes, restart)
                case "reload_all":
                    client.reload_all()
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
                case "projection":
                    client.projection(*command.value)
                case "export_snapshot":
                    path, purpose = command.value
                    client.export_snapshot(Path(path), str(purpose))
                case "export_project_file":
                    client.export_project_file(Path(command.value))
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
            client.fail_startup(error)
            if wait_bound_input and submitted_wait is not None:
                self.events.put(FrontendEvent("interaction_rejected", submitted_wait))
            if command.kind == "export_snapshot":
                client.pending_export = None
                client.pending_export_kind = None
                client.pending_export_message = None
                self.events.put(FrontendEvent("snapshot_export_finished", False))
            elif command.kind == "export_project_file":
                client.pending_export = None
                client.pending_export_kind = None
                client.pending_export_message = None
                client.pending_project_file_export_path = None
                self.events.put(FrontendEvent("project_file_export_finished", False))
            elif command.kind == "export_diagnosis":
                client._finish_diagnosis_export(False, str(error))
            self.events.put(FrontendEvent("error", str(error)))

    def _load_project(self, root: Path) -> None:
        if self.client is None:
            return
        self.client.begin_startup_attempt(project_file=False)
        self.events.put(FrontendEvent("status", f"正在扫描 {root}…"))
        bundle = ProjectBundle.scan_quick(root, 1, self._emit_scan_progress)
        restore = None
        if self.initial_state is not None:
            path, purpose = self.initial_state
            resolved = path.expanduser().resolve(strict=True)
            restore = (resolved, resolved.read_bytes(), purpose)
            self.initial_state = None
        self.client.recreate(bundle, restore)

    def _load_project_file(self, path: Path) -> None:
        if self.client is None:
            return
        self.client.begin_startup_attempt(project_file=True)
        resolved = path.expanduser().resolve(strict=True)
        payload = resolved.read_bytes()
        manifest = self.client.abi.project_file_manifest(payload)
        bundle = ProjectBundle.from_project_file_manifest(resolved, manifest)
        self.events.put(FrontendEvent("status", f"正在载入项目文件 {resolved}…"))
        self.client.recreate(bundle, project_file_bytes=payload)

    def _emit_scan_progress(self, completed: int, total: int) -> None:
        self.events.put(FrontendEvent("project_progress", (0, completed, total)))

    def _emit_project_progress(self, stage: int, completed: int, total: int) -> None:
        self.events.put(FrontendEvent("project_progress", (stage, completed, total)))

    def stop(self) -> None:
        self.send("force_stop")
