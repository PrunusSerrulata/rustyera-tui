from __future__ import annotations

import queue
import time
from pathlib import Path
from typing import Callable

import pytest

from rustyera_tui.abi import DEFAULT_MAXIMUM_VM_INSTRUCTIONS, AbiError, discover_library
from rustyera_tui.project import StorageBackend
from rustyera_tui.runtime import FrontendCommand, FrontendEvent, RuntimeClient, RuntimeWorker

try:
    RUNTIME_LIBRARY = discover_library()
except AbiError:
    RUNTIME_LIBRARY = None


def test_default_drive_budget_keeps_the_caller_pump_cooperative() -> None:
    assert DEFAULT_MAXIMUM_VM_INSTRUCTIONS == 100_000


def test_worker_applies_backpressure_to_presentation_events() -> None:
    worker = RuntimeWorker(None, None)

    assert worker.events.maxsize == 4_096


def test_compiled_cache_persistence_waits_until_the_deferred_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.cache_refresh_pending = True
    client.cache_ready = False
    client.cache_refresh_after_ns = 100
    client.pending_export = None
    requested: list[str] = []
    client._refresh_compiled_cache = requested.append  # type: ignore[method-assign]

    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 99)
    client.maybe_refresh_compiled_cache()
    assert requested == []

    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 100)
    client.maybe_refresh_compiled_cache()
    assert requested == ["background"]


def test_gameplay_input_defers_pending_cache_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.cache_refresh_pending = True
    client.cache_refresh_after_ns = 100

    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 500)
    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 50)
    client.defer_compiled_cache_refresh()

    assert client.cache_refresh_after_ns == 550

    client.cache_refresh_pending = False
    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 1_000)
    client.defer_compiled_cache_refresh()
    assert client.cache_refresh_after_ns == 550


def test_state_import_uses_large_contiguous_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(RuntimeClient)
    client.import_bytes = b"123456789"
    client.import_transfer_id = None
    commands: list[tuple[int, dict[int, object]]] = []
    client.send_runtime = lambda tag, value: commands.append((tag, value))  # type: ignore[method-assign]
    monkeypatch.setattr("rustyera_tui.runtime.STATE_IMPORT_CHUNK_BYTES", 4)

    client._handle_import_accepted({0: 7})

    assert [(tag, value.get(1), len(value.get(2, b""))) for tag, value in commands] == [
        (64, 0, 4),
        (64, 4, 4),
        (64, 8, 1),
        (65, None, 0),
    ]


def wait_for(
    worker: RuntimeWorker,
    predicate: Callable[[FrontendEvent], bool],
    timeout: float = 15,
) -> FrontendEvent:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = worker.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if predicate(event):
            return event
        if event.kind in ("error", "runtime_fault"):
            raise AssertionError(event.value)
    raise AssertionError("timed out waiting for runtime event")


def wait_for_path(worker: RuntimeWorker, path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    observed: list[FrontendEvent] = []
    while time.monotonic() < deadline:
        if path.is_file():
            return
        try:
            event = worker.events.get(timeout=0.02)
        except queue.Empty:
            continue
        observed.append(event)
        if event.kind in ("error", "runtime_fault"):
            raise AssertionError(event.value)
    client = worker.client
    state = (
        None
        if client is None
        else {
            "pending": client.cache_refresh_pending,
            "ready": client.cache_ready,
            "after": client.pending_cache_after,
            "export_kind": client.pending_export_kind,
            "export_message": client.pending_cache_export_message,
        }
    )
    raise AssertionError(f"timed out waiting for {path}; state={state}; events={observed[-20:]}")


def test_title_and_snapshot_restore_do_not_scan_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        def __init__(self) -> None:
            self.bundle = type("Bundle", (), {"root": tmp_path})()
            self.commands: list[tuple[int, object]] = []
            self.restored: Path | None = None

        def send_runtime(self, tag: int, value: object) -> None:
            self.commands.append((tag, value))

        def restore_snapshot(self, path: Path) -> None:
            self.restored = path

    snapshot = tmp_path / "state.snapshot"
    snapshot.write_bytes(b"snapshot")
    worker = RuntimeWorker(None, None)
    client = Client()
    worker.client = client  # type: ignore[assignment]
    monkeypatch.setattr(
        "rustyera_tui.runtime.ProjectBundle.scan",
        lambda *_args, **_kwargs: pytest.fail("project scan must not run"),
    )

    worker._process_command(FrontendCommand("return_title"))
    worker._process_command(FrontendCommand("restore_snapshot", snapshot))

    assert client.commands == [(23, {})]
    assert client.restored == snapshot.resolve()


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_relaunch_uses_the_persistent_compiled_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 0)
    project = Path(__file__).parent / "fixtures" / "minimal"
    cache_path = StorageBackend(project).compiled_cache_path()

    first = RuntimeWorker(RUNTIME_LIBRARY, project)
    first.start()
    try:
        wait_for(first, lambda event: event.kind == "project_loaded")
        wait_for(first, lambda event: event.kind == "wait" and event.value is not None)
        wait_for_path(first, cache_path)
    finally:
        first.stop()
        first.join(timeout=5)

    second = RuntimeWorker(RUNTIME_LIBRARY, project)
    second.start()
    try:
        cache_hit = wait_for(
            second,
            lambda event: event.kind == "log"
            and "runtime.compiled_cache_hit" in str(event.value),
        )
        assert "compiled_cache_hit" in cache_hit.value
    finally:
        second.stop()
        second.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_loads_starts_and_serves_debug_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 0)
    project = Path(__file__).parent / "fixtures" / "minimal"
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for(worker, lambda event: event.kind == "project_loaded")
        wait = wait_for(worker, lambda event: event.kind == "wait" and event.value is not None)
        assert wait.value[1] == 0
        wait_for_path(worker, StorageBackend(project).compiled_cache_path())

        worker.send("restart")
        cache_hit = wait_for(
            worker,
            lambda event: event.kind == "log" and "runtime.compiled_cache_hit" in str(event.value),
        )
        assert "compiled_cache_hit" in cache_hit.value
        wait_for(worker, lambda event: event.kind == "wait" and event.value is not None)

        snapshot_path = tmp_path / "runtime.snapshot"
        worker.send("export_snapshot", snapshot_path)
        exported = wait_for(
            worker,
            lambda event: event.kind == "snapshot_export_finished",
        )
        assert exported.value is True
        assert snapshot_path.stat().st_size > 0

        with monkeypatch.context() as context:
            context.setattr(
                "rustyera_tui.runtime.ProjectBundle.scan",
                lambda *_args, **_kwargs: pytest.fail(
                    "snapshot restore must not scan the project"
                ),
            )
            worker.send("restore_snapshot", snapshot_path)
            wait_for(
                worker,
                lambda event: event.kind == "status" and "正在恢复 VM" in str(event.value),
            )
            wait_for(worker, lambda event: event.kind == "wait" and event.value is not None)

        worker.send("reload_all")
        reloaded = wait_for(
            worker,
            lambda event: event.kind == "status" and "热重载完成" in str(event.value),
        )
        assert "完成" in reloaded.value

        worker.send("debug_enable")
        wait_for(worker, lambda event: event.kind == "debug_enabled" and event.value)
        worker.send("debug_action", ("variables", None))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        response = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "variables",
        )
        pending, response_tag, fields = response.value
        assert pending == "variables"
        assert response_tag == 1
        assert isinstance(fields[0].get(1), list)
        descriptor = fields[0][1][0]
        worker.send("debug_action", ("read_variable", descriptor))
        value = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "variable_value",
        )
        assert value.value[1] == 2

        worker.send("debug_action", ("fibers", None))
        fibers = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "fibers",
        ).value[2][0]
        assert fibers[1]
        fiber_id = fibers[1][0][0]
        worker.send("debug_action", ("call_stack", fiber_id))
        stack = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "call_stack",
        ).value[2][0]
        assert stack[2]
        worker.send("debug_action", ("operand_stack", (fiber_id, stack[2][0][0])))
        operands = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "operand_stack",
        )
        assert operands.value[1] == 7

        worker.send("debug_action", ("console_evaluate", "1 + 2"))
        console = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "console",
        )
        assert console.value[1] == 8
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_single_step_crosses_input_wait_without_rejected_commands(
    tmp_path: Path,
) -> None:
    project = tmp_path / "single-step-project"
    project.mkdir()
    (project / "main.erb").write_text(
        "@SYSTEM_TITLE\nPRINTL before\nWAIT\nPRINTL after\nWAIT\nRETURN\n",
        encoding="utf-8",
    )
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for(worker, lambda event: event.kind == "wait" and event.value is not None)
        worker.send("debug_enable")
        wait_for(worker, lambda event: event.kind == "debug_enabled" and event.value)
        worker.send("debug_single_step", True)
        worker.send("submit_text", "")

        stops: list[FrontendEvent] = []
        for _ in range(8):
            stopped = wait_for(worker, lambda event: event.kind == "debug_stopped")
            stops.append(stopped)
            source = stopped.value.get(3)
            assert source is not None
            assert source.get(0) == "main.erb"
            if stopped.value[1][0] == 3:
                break
            worker.send("debug_step")
        else:
            pytest.fail("single stepping did not reach the next input host wait")

        wait_for(worker, lambda event: event.kind == "phase" and event.value == 5)

        worker.send("debug_single_step", False)
        worker.send("debug_action", ("console_execute", "RESULT = 7"))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        console = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "console",
        )
        assert console.value[1] == 8
        worker.send("debug_surface_closed", "console")
        wait_for(worker, lambda event: event.kind == "phase" and event.value == 5)

        worker.send("debug_action", ("pause_only", None))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        worker.send("debug_disable")
        resumed = wait_for(worker, lambda event: event.kind == "phase")
        assert resumed.value == 5
        disabled = wait_for(
            worker,
            lambda event: event.kind == "debug_enabled" and not event.value,
        )
        assert disabled.value is False
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_projects_three_channel_background_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "background-project"
    project.mkdir()
    (project / "main.erb").write_text(
        "@SYSTEM_TITLE\nSETBGCOLOR 1, 24, 60\nWAIT\nRESETBGCOLOR\nWAIT\nRETURN\n",
        encoding="utf-8",
    )
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        blue = {0: 1, 1: 24, 2: 60, 3: 255}
        black = {0: 0, 1: 0, 2: 0, 3: 255}
        wait_for(
            worker,
            lambda event: event.kind == "presentation_snapshot"
            and event.value.get(6, {}).get(2) == blue,
        )
        wait_for(worker, lambda event: event.kind == "wait" and event.value is not None)
        worker.send("submit_text", "")
        wait_for(
            worker,
            lambda event: event.kind == "presentation_delta"
            and any(
                operation[0] == 8 and operation[1][0].get(2) == black
                for operation in event.value[2]
            ),
        )
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_reports_a_terminal_runtime_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "fault-project"
    project.mkdir()
    (project / "main.erb").write_text(
        '@SYSTEM_TITLE\nRESULT = CSVNAME(999) == ""\nRETURN\n', encoding="utf-8"
    )
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        fault = wait_for(worker, lambda event: event.kind == "runtime_fault")
        assert "character CSV number 999 does not exist" in fault.value.message
        assert fault.value.function == "SYSTEM_TITLE"
    finally:
        worker.stop()
        worker.join(timeout=3)
