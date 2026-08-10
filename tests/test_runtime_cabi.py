from __future__ import annotations

import queue
import shutil
import tarfile
import time
from pathlib import Path
from typing import Callable

import pytest
import zstandard

from rustyera_tui.abi import DEFAULT_MAXIMUM_VM_INSTRUCTIONS, AbiError, RuntimeAbi, discover_library
from rustyera_tui.project import ProjectBundle, StorageBackend
from rustyera_tui.runtime import (
    FrontendCommand,
    FrontendEvent,
    PresentationBatch,
    RuntimeClient,
    RuntimeWorker,
)

try:
    RUNTIME_LIBRARY = discover_library(resource_directory=Path(__file__).parents[1])
except AbiError:
    RUNTIME_LIBRARY = None


def test_default_drive_budget_keeps_the_caller_pump_cooperative() -> None:
    assert DEFAULT_MAXIMUM_VM_INSTRUCTIONS == 100_000


def test_compiled_cache_uses_the_abi_staging_fast_path() -> None:
    client = object.__new__(RuntimeClient)
    staged: list[bytes] = []
    submitted: list[int] = []
    fallback: list[tuple[bytes, int, str]] = []
    client.abi = type(
        "Abi",
        (),
        {"stage_compiled_cache": lambda _self, payload: staged.append(payload) or 41},
    )()
    client._submit_project = submitted.append  # type: ignore[method-assign]
    client._begin_import = lambda payload, kind, purpose: fallback.append(  # type: ignore[method-assign]
        (payload, kind, purpose)
    )

    client._stage_project_cache(b"cache", "project_cache")

    assert staged == [b"cache"]
    assert submitted == [41]
    assert fallback == []


def test_compiled_cache_falls_back_to_protocol_import_for_an_older_abi() -> None:
    client = object.__new__(RuntimeClient)
    submitted: list[int] = []
    fallback: list[tuple[bytes, int, str]] = []
    client.abi = type("Abi", (), {"stage_compiled_cache": lambda _self, _payload: None})()
    client._submit_project = submitted.append  # type: ignore[method-assign]
    client._begin_import = lambda payload, kind, purpose: fallback.append(  # type: ignore[method-assign]
        (payload, kind, purpose)
    )

    client._stage_project_cache(b"cache", "project_cache")

    assert submitted == []
    assert fallback == [(b"cache", 2, "project_cache")]


def test_compiled_cache_file_uses_runtime_owned_staging_without_python_bytes() -> None:
    client = object.__new__(RuntimeClient)
    staged: list[Path] = []
    submitted: list[int] = []
    client.abi = type(
        "Abi",
        (),
        {"stage_compiled_cache_file": lambda _self, path: staged.append(path) or 43},
    )()
    client._submit_project = submitted.append  # type: ignore[method-assign]

    path = Path("cache-does-not-need-to-be-read-by-runtime-client")
    client._stage_project_cache_file(path, "project_cache")

    assert staged == [path]
    assert submitted == [43]


def test_compiled_cache_file_falls_back_to_bytes_for_abi_34(tmp_path: Path) -> None:
    client = object.__new__(RuntimeClient)
    path = tmp_path / "compiled-project.reraproj"
    path.write_bytes(b"cache")
    staged: list[bytes] = []
    submitted: list[int] = []
    client.abi = type(
        "Abi",
        (),
        {
            "stage_compiled_cache_file": lambda _self, _path: None,
            "stage_compiled_cache": lambda _self, payload: staged.append(payload) or 44,
        },
    )()
    client._submit_project = submitted.append  # type: ignore[method-assign]

    client._stage_project_cache_file(path, "project_cache")

    assert staged == [b"cache"]
    assert submitted == [44]


def test_compiled_cache_file_io_failure_falls_back_to_materialized_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    bundle = ProjectBundle.scan_quick(tmp_path)
    cache_path = StorageBackend(tmp_path).compiled_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"cache")
    client = object.__new__(RuntimeClient)
    client.pending_bundle = bundle
    client.storage = StorageBackend(tmp_path)
    client.allow_compiled_cache_load = True
    client.events = queue.Queue()

    def fail_stage(_self: object, _path: Path) -> int:
        raise OSError("short read")

    client.abi = type(
        "Abi",
        (),
        {"stage_compiled_cache_file": fail_stage},
    )()
    client._project_scan_progress = lambda _completed, _total: None  # type: ignore[method-assign]
    submitted: list[int | None] = []
    client._submit_project = submitted.append  # type: ignore[method-assign]

    client._stage_persistent_cache_or_source()

    assert client.pending_bundle.is_materialized
    assert submitted == [None]
    events = list(client.events.queue)
    assert any(event.kind == "log" and "short read" in str(event.value) for event in events)


def test_runtime_library_is_discovered_in_the_resource_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_RUNTIME_LIBRARY", raising=False)
    monkeypatch.setattr("rustyera_tui.abi.sys.platform", "darwin")
    library = tmp_path / "libera_runtime_capi.dylib"
    library.write_bytes(b"library")

    assert discover_library(resource_directory=tmp_path) == library


def test_runtime_library_is_discovered_beside_the_packaged_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_RUNTIME_LIBRARY", raising=False)
    monkeypatch.setattr("rustyera_tui.abi.sys.platform", "darwin")
    module = tmp_path / "rustyera_tui" / "abi.py"
    module.parent.mkdir()
    module.write_text("", encoding="utf-8")
    library = module.parent / "libera_runtime_capi.dylib"
    library.write_bytes(b"library")
    monkeypatch.setattr("rustyera_tui.abi.__file__", str(module))

    assert discover_library(resource_directory=tmp_path) == library


def test_worker_applies_backpressure_to_presentation_events() -> None:
    worker = RuntimeWorker(None, None)

    assert worker.events.maxsize == 4_096


def test_startup_milestones_cover_waiting_external_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.phase = 0
    client.epoch = None
    client.startup_attempt = 0
    client.startup_scenario = "cold"
    client.startup_active = False
    client.startup_start_submitted = False
    client.startup_first_phase_reported = False
    client._presentation_boundary_dirty = False
    commands: list[tuple[int, object]] = []
    milestones: list[tuple[str, dict[str, object]]] = []
    client.send_runtime = lambda tag, value: commands.append((tag, value))  # type: ignore[method-assign]
    monkeypatch.setattr(
        "rustyera_tui.runtime.emit_startup_milestone",
        lambda event, **fields: milestones.append((event, fields)),
    )

    client.begin_startup_attempt(project_file=False)
    client._submit_start({0: "new-game"})
    client._handle_runtime(21, {0: 6, 2: 4}, None)

    assert commands == [(20, {0: "new-game"})]
    assert [event for event, _fields in milestones] == [
        "attempt_started",
        "start_submitted",
        "first_game_phase",
    ]
    assert milestones[-1][1]["phase"] == 6
    assert client.startup_active is False


def test_terminal_runtime_phase_fails_active_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.phase = 0
    client.epoch = None
    client.startup_attempt = 0
    client.startup_scenario = "cold"
    client.startup_active = False
    client.startup_start_submitted = False
    client.startup_first_phase_reported = False
    client._presentation_boundary_dirty = False
    milestones: list[str] = []
    monkeypatch.setattr(
        "rustyera_tui.runtime.emit_startup_milestone",
        lambda event, **_fields: milestones.append(event),
    )

    client.begin_startup_attempt(project_file=True)
    client._handle_runtime(21, {0: 11, 2: 2}, None)

    assert milestones == ["attempt_started", "failed"]
    assert client.startup_active is False


def test_project_scan_failure_terminates_the_attempt_before_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        def begin_startup_attempt(self, *, project_file: bool) -> None:
            self.events.append(("begin", project_file))

        def fail_startup(self, error: object) -> None:
            self.events.append(("failed", str(error)))

    worker = RuntimeWorker(None, None)
    client = Client()
    worker.client = client  # type: ignore[assignment]
    monkeypatch.setattr(
        "rustyera_tui.worker.ProjectBundle.scan_quick",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("scan failed")),
    )

    worker._process_command(FrontendCommand("load_project", tmp_path))

    assert client.events == [("begin", False), ("failed", "scan failed")]


def test_project_file_read_failure_terminates_the_attempt(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        def begin_startup_attempt(self, *, project_file: bool) -> None:
            self.events.append(("begin", project_file))

        def fail_startup(self, error: object) -> None:
            self.events.append(("failed", str(error)))

    worker = RuntimeWorker(None, None)
    client = Client()
    worker.client = client  # type: ignore[assignment]

    worker._process_command(FrontendCommand("load_project_file", tmp_path / "missing.reraproj"))

    assert client.events[0] == ("begin", True)
    assert client.events[1][0] == "failed"
    assert "missing.reraproj" in str(client.events[1][1])


def test_failed_wait_bound_worker_command_releases_the_app_input_gate() -> None:
    wait = {0: 17, 1: 2, 11: {0: 1, 1: 4}}

    class Client:
        active_wait = wait

        @staticmethod
        def defer_compiled_cache_refresh() -> None:
            pass

        @staticmethod
        def submit_text(_text: str) -> None:
            raise RuntimeError("submission failed")

        @staticmethod
        def fail_startup(_error: object) -> None:
            pass

    worker = RuntimeWorker(None, None)
    worker.client = Client()  # type: ignore[assignment]

    worker._process_command(FrontendCommand("submit_text", "412"))

    assert worker.events.get_nowait() == FrontendEvent("interaction_rejected", wait)
    assert worker.events.get_nowait() == FrontendEvent("error", "submission failed")


def test_worker_delivers_presentation_and_wait_as_one_atomic_batch() -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client._pending_presentation_events = [
        ("delta", {0: 1, 1: 2, 2: []}),
        ("delta", {0: 2, 1: 3, 2: []}),
    ]
    client._wait_event_dirty = True
    client._presentation_boundary_dirty = False
    client.active_wait = {0: 7, 1: 0, 11: {0: 1, 1: 9}}

    client._flush_presentation_events()

    event = client.events.get_nowait()
    assert event.kind == "presentation_batch"
    assert event.value == PresentationBatch(
        None,
        {0: 1, 1: 3, 2: []},
        client.active_wait,
        True,
    )
    assert client.events.empty()


def test_compiled_cache_persistence_waits_until_the_deferred_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.cache_refresh_pending = True
    client.cache_ready = False
    client.cache_refresh_after_ns = 100
    client.pending_export = None
    client.full_project_export = None
    client.pending_diagnosis = None
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
        if event.kind in ("error", "runtime_error", "runtime_fault"):
            raise AssertionError(event.value)
    raise AssertionError("timed out waiting for runtime event")


def wait_for_input(worker: RuntimeWorker) -> dict[int, object]:
    event = wait_for(
        worker,
        lambda candidate: (
            candidate.kind == "presentation_batch" and candidate.value.active_wait is not None
        ),
    )
    return event.value.active_wait


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
        if event.kind in ("error", "runtime_error", "runtime_fault"):
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
        wait_for_input(first)
        wait_for_path(first, cache_path)
    finally:
        first.stop()
        first.join(timeout=5)

    second = RuntimeWorker(RUNTIME_LIBRARY, project)
    second.start()
    try:
        cache_hit = wait_for(
            second,
            lambda event: event.kind == "log" and "runtime.compiled_cache_hit" in str(event.value),
        )
        assert "compiled_cache_hit" in cache_hit.value
    finally:
        second.stop()
        second.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_exports_appends_and_reopens_packaged_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 0)
    project = tmp_path / "minimal"
    shutil.copytree(Path(__file__).parent / "fixtures" / "minimal", project)
    cache_path = StorageBackend(project).compiled_cache_path()
    project_file = tmp_path / "minimal.reraproj"
    source_worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    source_worker.start()
    try:
        wait_for(source_worker, lambda event: event.kind == "project_loaded")
        wait_for_input(source_worker)
        wait_for_path(source_worker, cache_path)
        source_worker.send("export_project_file", project_file)
        finished = wait_for(
            source_worker, lambda event: event.kind == "project_file_export_finished"
        )
        assert finished.value is True
        wait_for_path(source_worker, project_file)
    finally:
        source_worker.stop()
        source_worker.join(timeout=5)

    original = project_file.read_bytes()
    assert original.startswith(b"RERAPROJ")
    abi = RuntimeAbi(RUNTIME_LIBRARY, resource_directory=project)
    try:
        manifest = abi.project_file_manifest(original)
        bundle = ProjectBundle.from_project_file_manifest(project_file, manifest)
        bundle.write_configuration(
            b"",
            '[text]\nreplace_full_width_spaces = true\ncharacter_width_mode = "ambiguous_wide"\n',
            abi.prepare_project_configuration_update,
        )
    finally:
        abi.destroy_session()

    updated = project_file.read_bytes()
    assert updated.startswith(original)
    assert 0 < len(updated) - len(original) < 1024
    packaged_worker = RuntimeWorker(RUNTIME_LIBRARY, None, initial_project_file=project_file)
    packaged_worker.start()
    try:
        configuration = wait_for(packaged_worker, lambda event: event.kind == "configuration")
        snapshot, read_only = configuration.value
        assert read_only is False
        assert snapshot.effective_value("ReplaceFullWidthSpaces", "NO") == "YES"
        assert snapshot.effective_value("CharacterWidthMode", "AUTOMATIC") == "AMBIGUOUS_WIDE"
        wait_for_input(packaged_worker)
    finally:
        packaged_worker.stop()
        packaged_worker.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_diagnosis_contains_a_parseable_full_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "minimal"
    shutil.copytree(Path(__file__).parent / "fixtures" / "minimal", project)
    target = tmp_path / "minimal-diagnosis.tar.zst"
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for(worker, lambda event: event.kind == "project_loaded")
        wait_for_input(worker)
        worker.send("export_diagnosis", (target, "complete log\n", "minimal"))
        finished = wait_for(worker, lambda event: event.kind == "diagnosis_export_finished")
        assert finished.value == (True, str(target))
        wait_for_path(worker, target)
    finally:
        worker.stop()
        worker.join(timeout=5)

    with target.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                project_file = next(
                    archive.extractfile(member).read()
                    for member in archive
                    if member.isfile() and member.name.endswith(".reraproj")
                )
    assert project_file.startswith(b"RERAPROJ")
    abi = RuntimeAbi(RUNTIME_LIBRARY, resource_directory=project)
    try:
        manifest = abi.project_file_manifest(project_file)
    finally:
        abi.destroy_session()
    assert manifest[0] == 1
    assert manifest[1]


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
        wait = wait_for_input(worker)
        assert wait[1] == 0
        wait_for_path(worker, StorageBackend(project).compiled_cache_path())

        worker.send("restart")
        cache_hit = wait_for(
            worker,
            lambda event: event.kind == "log" and "runtime.compiled_cache_hit" in str(event.value),
        )
        assert "compiled_cache_hit" in cache_hit.value
        wait_for_input(worker)

        snapshot_path = tmp_path / "runtime.snapshot"
        worker.send("export_snapshot", (snapshot_path, "normal"))
        exported = wait_for(
            worker,
            lambda event: event.kind == "snapshot_export_finished",
        )
        assert exported.value is True
        assert snapshot_path.stat().st_size > 0

        with monkeypatch.context() as context:
            context.setattr(
                "rustyera_tui.runtime.ProjectBundle.scan",
                lambda *_args, **_kwargs: pytest.fail("snapshot restore must not scan the project"),
            )
            worker.send("restore_snapshot", snapshot_path)
            wait_for(
                worker,
                lambda event: event.kind == "status" and "正在恢复 VM" in str(event.value),
            )
            wait_for_input(worker)

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
        wait_for_input(worker)
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
        initial = wait_for(
            worker,
            lambda event: (
                event.kind == "presentation_batch"
                and event.value.snapshot is not None
                and event.value.snapshot.get(6, {}).get(2) == blue
                and event.value.active_wait is not None
            ),
        )
        assert initial.value.render
        worker.send("submit_text", "")
        wait_for(
            worker,
            lambda event: (
                event.kind == "presentation_batch"
                and event.value.delta is not None
                and any(
                    operation[0] == 8 and operation[1][0].get(2) == black
                    for operation in event.value.delta[2]
                )
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
