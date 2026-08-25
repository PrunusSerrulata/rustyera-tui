from __future__ import annotations

from runtime_cabi_test_support import (
    FrontendCommand,
    FrontendEvent,
    GameInformation,
    Path,
    ProjectBundle,
    RuntimeClient,
    RuntimeWorker,
    pytest,
    queue,
)


def test_project_file_read_failure_terminates_the_attempt(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        def begin_startup_attempt(self, *, project_file: bool) -> None:
            self.events.append(("begin", project_file))

        def begin_session_reset(self) -> None:
            pass

        def fail_startup(self, error: object) -> None:
            self.events.append(("failed", str(error)))

    worker = RuntimeWorker(None, None)
    client = Client()
    worker.client = client  # type: ignore[assignment]

    worker._process_command(FrontendCommand("load_project_file", tmp_path / "missing.reraproj"))

    assert client.events[0] == ("begin", True)
    assert client.events[1][0] == "failed"
    assert "missing.reraproj" in str(client.events[1][1])


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


def test_worker_routes_scoped_reload_commands(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.bundle = type("Bundle", (), {"root": tmp_path})()
            self.reloaded: list[tuple[str, Path | None]] = []

        def reload_all(self) -> None:
            self.reloaded.append(("all", None))

        def reload_folder(self, path: Path) -> None:
            self.reloaded.append(("folder", path))

        def reload_file(self, path: Path) -> None:
            self.reloaded.append(("file", path))

    worker = RuntimeWorker(None, None)
    client = Client()
    worker.client = client  # type: ignore[assignment]

    worker._process_command(FrontendCommand("reload_all"))
    worker._process_command(FrontendCommand("reload_folder", tmp_path / "ERB"))
    worker._process_command(FrontendCommand("reload_file", tmp_path / "main.erb"))

    assert client.reloaded == [
        ("all", None),
        ("folder", tmp_path / "ERB"),
        ("file", tmp_path / "main.erb"),
    ]


def test_no_change_reload_refreshes_the_frontend_baseline_without_runtime_work(
    tmp_path: Path,
) -> None:
    current = ProjectBundle(tmp_path, 7, {})
    candidate = ProjectBundle(tmp_path, 8, {})
    client = object.__new__(RuntimeClient)
    client.bundle = current
    client.reload_candidate = None
    client.events = queue.Queue()
    submitted: list[tuple[int, object]] = []
    client.send_runtime = lambda tag, value: submitted.append((tag, value))  # type: ignore[method-assign]

    client._submit_reload(candidate, {0: 7, 1: 8, 2: []}, "0 个文件变更")

    assert client.bundle is candidate
    assert candidate.revision == 7
    assert client.reload_candidate is None
    assert submitted == []
    status = client.events.get_nowait()
    assert status.kind == "status"
    assert "热重载完成" in status.value


def test_failed_reload_keeps_the_active_bundle_for_later_diagnosis(tmp_path: Path) -> None:
    active = ProjectBundle(tmp_path, 7, {})
    candidate = ProjectBundle(tmp_path, 8, {})
    client = object.__new__(RuntimeClient)
    client.bundle = active
    client.pending_bundle = None
    client.reload_candidate = candidate
    client.reload_message_id = 91
    client.events = queue.Queue()
    client.fail_startup = lambda _reason: None  # type: ignore[method-assign]

    client._handle_project_report({0: 8, 1: False, 2: []})

    assert client.bundle is active
    assert client.reload_candidate is None
    assert client.reload_message_id is None
    assert client.events.get_nowait().kind == "runtime_error"


def test_successful_reload_projects_game_information_from_the_protocol(tmp_path: Path) -> None:
    active = ProjectBundle(tmp_path, 7, {})
    candidate = ProjectBundle(tmp_path, 8, {})
    client = object.__new__(RuntimeClient)
    client.bundle = active
    client.pending_bundle = None
    client.reload_candidate = candidate
    client.reload_message_id = 91
    client.events = queue.Queue()
    client._publish_configuration = lambda _value: None  # type: ignore[method-assign]

    client._handle_project_report(
        {
            0: 8,
            1: True,
            2: [],
            5: {0: "Demo", 1: "   ", 2: "1.001", 3: None, 4: "Notes"},
        }
    )

    assert client.bundle is candidate
    assert client.reload_candidate is None
    assert client.reload_message_id is None
    events = [client.events.get_nowait(), client.events.get_nowait()]
    assert events[0].kind == "status"
    assert events[1] == FrontendEvent(
        "game_information",
        GameInformation(title="Demo", version="1.001", information="Notes"),
    )
