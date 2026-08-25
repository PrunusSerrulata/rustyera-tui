from __future__ import annotations

from runtime_cabi_test_support import (
    FrontendCommand,
    FrontendEvent,
    Path,
    PresentationBatch,
    ProjectBundle,
    RuntimeClient,
    RuntimeWorker,
    pytest,
    queue,
)
from rustyera_tui.presentation import PresentationEventAccumulator
from rustyera_tui.wire import variant


def test_worker_applies_backpressure_to_presentation_events() -> None:
    worker = RuntimeWorker(None, None)

    assert worker.events.maxsize == 4_096


def test_worker_notifies_after_each_successful_event_publish() -> None:
    worker = RuntimeWorker(None, None)
    notifications: list[bool] = []
    worker.set_event_notifier(lambda: notifications.append(True))

    worker.events.put_nowait(FrontendEvent("status", "ready"))
    worker.set_event_notifier(None)
    worker.events.put_nowait(FrontendEvent("status", "quiet"))

    assert notifications == [True]


def test_worker_notification_failure_does_not_lose_the_published_event() -> None:
    worker = RuntimeWorker(None, None)

    def fail_notification() -> None:
        raise RuntimeError("UI already stopped")

    worker.set_event_notifier(fail_notification)
    event = FrontendEvent("status", "retained")
    worker.events.put_nowait(event)

    assert worker.events.get_nowait() == event


def test_worker_shutdown_closes_once_when_event_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class FakeAbi:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr("rustyera_tui.worker.RuntimeAbi", FakeAbi)
    monkeypatch.setattr("rustyera_tui.runtime.RuntimeClient", FakeClient)
    worker = RuntimeWorker(None, None)
    for _ in range(worker.events.maxsize):
        worker.events.put_nowait(FrontendEvent("status", "queued"))
    worker.start()

    worker.shutdown()
    worker.shutdown()

    assert not worker.is_alive()
    assert closed == [True]
    assert "worker_stopped" in {
        worker.events.get_nowait().kind for _ in range(worker.events.qsize())
    }


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


def test_runtime_progress_records_structured_core_phase_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.pending_diagnosis = None
    client.pending_export_kind = None
    client.startup_attempt = 3
    client.startup_host_durations = {}
    client.startup_core_durations = {}
    client._startup_core_phase_started = {}
    times = iter((1_000_000_000, 1_075_000_000))
    milestones: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: next(times))
    monkeypatch.setattr(
        "rustyera_tui.runtime.emit_startup_milestone",
        lambda event, **fields: milestones.append((event, fields)),
    )

    client.report_runtime_project_progress(11, 0, 1)
    client.report_runtime_project_progress(11, 1, 1)

    assert client.startup_core_durations == {"cache_decode_ms": 75.0}
    assert milestones == [
        (
            "core_phase",
            {
                "attempt_id": 3,
                "stage": 11,
                "phase": "cache_decode_ms",
                "duration_ms": 75.0,
            },
        )
    ]


def test_runtime_progress_ignores_duplicate_starts_and_supports_interleaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.pending_diagnosis = None
    client.pending_export_kind = None
    client.startup_attempt = 4
    client.startup_core_durations = {}
    client._startup_core_phase_started = {}
    times = iter((1_000_000_000, 1_010_000_000, 1_020_000_000, 1_050_000_000, 1_090_000_000))
    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: next(times))
    monkeypatch.setattr(
        "rustyera_tui.runtime.emit_startup_milestone", lambda *_args, **_kwargs: None
    )

    client.report_runtime_project_progress(3, 0, 10)
    client.report_runtime_project_progress(3, 0, 10)
    client.report_runtime_project_progress(4, 0, 10)
    client.report_runtime_project_progress(3, 10, 10)
    client.report_runtime_project_progress(4, 10, 10)

    assert client.startup_core_durations == {"parse_ms": 50.0, "analyze_ms": 70.0}


def test_runtime_progress_ignores_an_end_without_a_start_and_failure_clears_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.pending_diagnosis = None
    client.pending_export_kind = None
    client.startup_attempt = 5
    client.startup_scenario = "cold"
    client.startup_active = True
    client.startup_core_durations = {}
    client._startup_core_phase_started = {3: 1}
    monkeypatch.setattr(
        "rustyera_tui.runtime.emit_startup_milestone", lambda *_args, **_kwargs: None
    )

    client.report_runtime_project_progress(4, 10, 10)
    client.fail_startup("failed")

    assert client.startup_core_durations == {}
    assert client._startup_core_phase_started == {}


def test_project_scan_failure_terminates_the_attempt_before_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        def begin_startup_attempt(self, *, project_file: bool) -> None:
            self.events.append(("begin", project_file))

        def begin_session_reset(self) -> None:
            self.events.append(("reset", None))

        def fail_startup(self, error: object) -> None:
            self.events.append(("failed", str(error)))

        def record_host_metrics(self, _metrics: object) -> None:
            self.events.append(("metrics", None))

        def recreate(self, _bundle: ProjectBundle, _restore: object = None) -> None:
            self.events.append(("recreate", None))

    worker = RuntimeWorker(None, None)
    client = Client()
    worker.client = client  # type: ignore[assignment]
    attempts = iter((OSError("scan failed"), ProjectBundle(tmp_path, 1, {})))

    def scan(*_args: object, **_kwargs: object) -> ProjectBundle:
        result = next(attempts)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr("rustyera_tui.worker.ProjectBundle.scan_quick", scan)

    worker._process_command(FrontendCommand("load_project", tmp_path))

    assert client.events == [
        ("begin", False),
        ("reset", None),
        ("failed", "scan failed"),
    ]

    worker._process_command(FrontendCommand("load_project", tmp_path))

    assert client.events[-4:] == [
        ("begin", False),
        ("reset", None),
        ("metrics", None),
        ("recreate", None),
    ]


def test_packaged_project_uses_one_replacement_session_for_decode_and_hello(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FakeAbi:
        def submit(self, _data: bytes) -> None:
            calls.append("submit")

        def destroy_session(self) -> None:
            calls.append("destroy")

        def create_session(self) -> None:
            calls.append("create")

        def project_file_manifest(self, _payload: bytes) -> dict[int, object]:
            calls.append("decode")
            return {0: 1, 1: []}

    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"package")
    worker = RuntimeWorker(None, None)
    worker.client = RuntimeClient(FakeAbi(), worker.events)  # type: ignore[arg-type]
    calls.clear()

    worker._load_project_file(project_file)

    assert calls == ["destroy", "create", "decode", "submit"]
    assert worker.client.pending_bundle is not None
    assert worker.client.pending_bundle.project_file == project_file
    assert worker.client.can_pump


def test_packaged_manifest_decode_failure_destroys_replacement_once(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeAbi:
        def submit(self, _data: bytes) -> None:
            pass

        def destroy_session(self) -> None:
            calls.append("destroy")

        def create_session(self) -> None:
            calls.append("create")

        def project_file_manifest(self, _payload: bytes) -> dict[int, object]:
            raise RuntimeError("decode failed")

    project_file = tmp_path / "broken.reraproj"
    project_file.write_bytes(b"broken")
    worker = RuntimeWorker(None, None)
    worker.client = RuntimeClient(FakeAbi(), worker.events)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="decode failed"):
        worker._load_project_file(project_file)

    assert calls == ["destroy", "create", "destroy"]
    assert not worker.client.can_pump
    assert worker.client.pending_bundle is None


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


def test_failed_operation_sequence_export_releases_the_app_export_gate(tmp_path: Path) -> None:
    class Client:
        pending_export = (tmp_path / "input-replay.jsonl", bytearray(), None)
        pending_export_kind = 7
        pending_export_message = 41

        @staticmethod
        def export_input_replay(_path: Path) -> None:
            raise RuntimeError("export submission failed")

        @staticmethod
        def fail_startup(_error: object) -> None:
            pass

    worker = RuntimeWorker(None, None)
    worker.client = Client()  # type: ignore[assignment]

    worker._process_command(FrontendCommand("export_input_replay", tmp_path / "input-replay.jsonl"))

    assert worker.events.get_nowait() == FrontendEvent("input_replay_export_finished", False)
    assert worker.events.get_nowait() == FrontendEvent("error", "export submission failed")


def test_worker_delivers_presentation_and_wait_as_one_atomic_batch() -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client._pending_presentation = PresentationEventAccumulator()
    client._pending_presentation.add_delta({0: 1, 1: 2, 2: []})
    client._pending_presentation.add_delta({0: 2, 1: 3, 2: []})
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
    assert client._pending_presentation.take() == (None, None)


def test_worker_coalesces_running_presentation_across_pumps_until_visible_boundary() -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client._pending_presentation = PresentationEventAccumulator()
    client._pending_presentation.add_delta({0: 1, 1: 2, 2: []})
    client._wait_event_dirty = False
    client._presentation_boundary_dirty = False
    client.active_wait = None

    client._flush_presentation_events()

    assert client.events.empty()

    client._pending_presentation.add_delta({0: 2, 1: 3, 2: []})
    client.active_wait = {0: 7, 1: 0, 11: {0: 1, 1: 9}}
    client._wait_event_dirty = True
    client._flush_presentation_events()

    assert client.events.get_nowait() == FrontendEvent(
        "presentation_batch",
        PresentationBatch(None, {0: 1, 1: 3, 2: []}, client.active_wait, True),
    )
    assert client._pending_presentation.take() == (None, None)


def test_worker_clears_wait_without_publishing_staged_running_history() -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    delta = {0: 4, 1: 5, 2: []}
    client._pending_presentation = PresentationEventAccumulator()
    client._pending_presentation.add_delta(delta)
    client._wait_event_dirty = True
    client._presentation_boundary_dirty = False
    client.active_wait = None

    client._flush_presentation_events()

    assert client.events.get_nowait() == FrontendEvent(
        "presentation_batch", PresentationBatch(None, None, None, False)
    )
    client._presentation_boundary_dirty = True
    client._wait_event_dirty = False
    client._flush_presentation_events()
    assert client.events.get_nowait() == FrontendEvent(
        "presentation_batch", PresentationBatch(None, delta, None, True)
    )
    assert client._pending_presentation.take() == (None, None)


def test_unchanged_wait_does_not_enqueue_redundant_running_notifications() -> None:
    client = object.__new__(RuntimeClient)
    wait = {0: 7, 1: 0, 11: {0: 1, 1: 9}}
    client.active_wait = None
    client._wait_event_dirty = False

    client._handle_wait_change(variant(0, wait))
    assert client._wait_event_dirty

    client._wait_event_dirty = False
    client._handle_wait_change(variant(0, wait))
    assert not client._wait_event_dirty

    client._handle_wait_change(variant(2, 999))
    assert not client._wait_event_dirty

    client._handle_wait_change(variant(2, wait[0]))
    assert client._wait_event_dirty
    assert client.active_wait is None


@pytest.mark.parametrize(
    ("tag", "value"),
    [
        (21, {0: 10, 2: 4}),
        (22, {0: 0}),
        (32, variant(0, {0: 7, 1: 0, 11: {0: 1, 1: 9}})),
        (91, {0: True}),
        (92, {0: 4, 1: "fault"}),
    ],
    ids=("terminal-phase", "exit", "wait", "shutdown", "fault"),
)
def test_visible_runtime_boundaries_flush_staged_presentation_once(tag: int, value: object) -> None:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client._pending_presentation = PresentationEventAccumulator()
    delta = {0: 7, 1: 8, 2: []}
    client._pending_presentation.add_delta(delta)
    client._wait_event_dirty = False
    client._presentation_boundary_dirty = False
    client.active_wait = None
    client.phase = 0
    client.epoch = None
    client.startup_active = False
    client.startup_start_submitted = False
    client.startup_first_phase_reported = False
    client.pending_import = None

    client._handle_runtime(tag, value, None)
    client._flush_presentation_events()

    batches = []
    while not client.events.empty():
        event = client.events.get_nowait()
        if event.kind == "presentation_batch":
            batches.append(event.value)
    assert len(batches) == 1
    assert batches[0].delta == delta
    assert client._pending_presentation.take() == (None, None)
