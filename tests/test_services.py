from __future__ import annotations

import queue

import apsw
import pytest

from services_test_support import (
    Any,
    AtomicExportStream,
    FILE_RESOURCE,
    FrontendEvent,
    FullProjectExport,
    Path,
    ProjectBundle,
    ProjectFile,
    RuntimeClient,
    RuntimeFailure,
    ServiceLine,
    SimpleNamespace,
    StorageBackend,
    blake3,
    client_with_capture,
    encode,
    ready_payload,
    variant,
    _PendingExport,
    ExportStage,
)
from rustyera_tui.wire import (
    CHANNEL_RUNTIME,
    RUNTIME_VERSION,
    decode_envelope,
    encode_envelope,
    runtime_message,
)
from rustyera_tui.sql_provider import LIMITS, SqlProvider


def test_server_hello_reports_the_runtime_product_version() -> None:
    client, _ = client_with_capture()
    client.pending_bundle = None

    client._handle_runtime(1, {1: {0: 1, 1: 2}, 4: 7, 7: 1, 8: "0.6.0"}, None)

    assert client.events.get_nowait() == FrontendEvent("runtime_version", "0.6.0")


def test_first_envelope_of_new_epoch_resets_storage_before_dispatch(tmp_path: Path) -> None:
    client = object.__new__(RuntimeClient)
    client.epoch = 7
    client._runtime_epoch_transition = None
    client.expected_runtime_output = 0
    client.storage = StorageBackend(tmp_path)
    client._pending_sql_requests = {}
    client.sql_provider = SqlProvider()
    sql_provider = {0: 7, 1: 1}
    connection = {0: 7, 1: 2}
    client.sql_provider.handle(
        encode(
            {
                0: sql_provider,
                1: variant(
                    0,
                    connection,
                    "memory",
                    {0: variant(0), 1: "3.53.0", 2: 1},
                    variant(0),
                    dict(LIMITS),
                ),
            }
        ),
        client.storage,
        None,
    )
    client.sql_provider.handle(
        encode(
            {
                0: sql_provider,
                1: variant(1, connection, 3, "SELECT 1", []),
            }
        ),
        client.storage,
        None,
    )
    database = client.sql_provider.connections[(7, 2)].database
    client.storage.begin_epoch(7)
    client.storage.idempotent_results["old"] = variant(3)
    observed: list[tuple[int | None, list[str]]] = []
    client._handle_runtime = (  # type: ignore[method-assign]
        lambda _tag, _value, _correlation: observed.append(
            (client.storage.idempotency_epoch, list(client.storage.idempotent_results))
        )
    )
    envelope = encode_envelope(
        channel=CHANNEL_RUNTIME,
        channel_version=RUNTIME_VERSION,
        session={0: 1, 1: 2},
        sequence=0,
        message_id=1,
        correlation_id=None,
        payload_tag=42,
        payload=runtime_message(42, {}),
        epoch=8,
    )

    client._handle_envelope(envelope)

    assert observed == [(8, [])]
    assert client.sql_provider.connections == {}
    assert client.sql_provider.readers == {}
    with pytest.raises(apsw.ConnectionClosedError):
        database.get_autocommit()


def test_session_reset_releases_old_client_state_before_recreate(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeAbi:
        def submit(self, _data: bytes) -> None:
            calls.append("submit")

        def destroy_session(self) -> None:
            calls.append("destroy")

        def create_session(self) -> None:
            calls.append("create")

    client = RuntimeClient(FakeAbi(), queue.Queue())  # type: ignore[arg-type]
    old_bundle = ProjectBundle(
        tmp_path,
        1,
        {"main.erb": ProjectFile("main.erb", 2, variant(0, "old source"), bytes(32))},
    )
    client.bundle = old_bundle
    client.pending_bundle = old_bundle
    client.storage = StorageBackend(tmp_path)
    client.presentation.lines = [ServiceLine(1, True, 0, "old history")]

    client.begin_session_reset()

    assert calls[-1] == "destroy"
    assert client.bundle is None
    assert client.pending_bundle is None
    assert client.storage is None
    assert client.presentation.lines == []
    assert "session_reset" in {
        client.events.get_nowait().kind for _ in range(client.events.qsize())
    }

    replacement = ProjectBundle(tmp_path, 1, {})
    client.recreate(replacement)
    assert calls[-2:] == ["create", "submit"]
    assert client.pending_bundle is replacement
    assert client.storage is None  # The negotiated core resolver binds storage later.
    assert not client._session_reset_active


@pytest.mark.parametrize("failure", ["create", "hello"])
def test_recreate_failure_releases_partial_session_and_preserves_original_error(
    tmp_path: Path, failure: str
) -> None:
    class FakeAbi:
        def __init__(self) -> None:
            self.fail_create = False
            self.fail_submit = False
            self.active = True
            self.destroy_calls = 0

        def submit(self, _data: bytes) -> None:
            if self.fail_submit:
                raise RuntimeError("hello failed")

        def destroy_session(self) -> None:
            self.destroy_calls += 1
            self.active = False

        def create_session(self) -> None:
            self.active = True
            if self.fail_create:
                raise RuntimeError("create failed")

    abi = FakeAbi()
    client = RuntimeClient(abi, queue.Queue())  # type: ignore[arg-type]
    abi.fail_create = failure == "create"
    abi.fail_submit = failure == "hello"

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        client.recreate(ProjectBundle(tmp_path, 1, {}))

    assert not abi.active
    assert abi.destroy_calls == 2
    assert client._session_reset_active
    assert not client._replacement_session_prepared
    assert client.pending_bundle is None
    assert client.storage is None


def test_destroy_failure_clears_python_state_and_preserves_original_error(tmp_path: Path) -> None:
    class FakeAbi:
        def submit(self, _data: bytes) -> None:
            pass

        def destroy_session(self) -> None:
            raise RuntimeError("destroy failed")

    client = RuntimeClient(FakeAbi(), queue.Queue())  # type: ignore[arg-type]
    client.bundle = ProjectBundle(tmp_path, 1, {})
    client.storage = StorageBackend(tmp_path)

    with pytest.raises(RuntimeError, match="destroy failed"):
        client.begin_session_reset()

    assert client.bundle is None
    assert client.storage is None
    assert client._session_reset_active
    assert client._session_destroy_pending


def test_recreate_cleanup_failure_does_not_mask_hello_failure(tmp_path: Path) -> None:
    class FakeAbi:
        fail_submit = False
        destroy_calls = 0

        def submit(self, _data: bytes) -> None:
            if self.fail_submit:
                raise RuntimeError("hello failed")

        def destroy_session(self) -> None:
            self.destroy_calls += 1
            if self.destroy_calls == 2:
                raise RuntimeError("cleanup failed")

        def create_session(self) -> None:
            pass

    abi = FakeAbi()
    client = RuntimeClient(abi, queue.Queue())  # type: ignore[arg-type]
    abi.fail_submit = True

    with pytest.raises(RuntimeError, match="hello failed") as failure:
        client.recreate(ProjectBundle(tmp_path, 1, {}))

    assert any("cleanup failed" in note for note in getattr(failure.value, "__notes__", ()))
    assert client._session_destroy_pending
    assert client._session_reset_active


def test_project_submission_stages_manifest_once_and_sends_a_small_load_request() -> None:
    client, captured = client_with_capture()
    manifest = {0: 1, 1: [{0: "main.erb", 1: 2, 2: variant(0, "@MAIN\nRETURN\n")}]}
    staged: list[dict[int, Any]] = []
    client.pending_bundle = SimpleNamespace(
        is_materialized=True,
        identity=lambda: {0: 1, 1: bytes(32)},
        manifest=lambda: manifest,
    )
    client.abi.stage_project_manifest = lambda value: staged.append(value) or True

    client._submit_project(None)

    assert staged == [manifest]
    assert captured == [(19, {0: {0: 1, 1: bytes(32)}})]


def test_project_submission_keeps_the_legacy_envelope_fallback() -> None:
    client, captured = client_with_capture()
    manifest = {0: 1, 1: []}
    client.pending_bundle = SimpleNamespace(
        is_materialized=True,
        identity=lambda: {0: 1, 1: bytes(32)},
        manifest=lambda: manifest,
    )
    client.abi.stage_project_manifest = lambda _value: False

    client._submit_project(None)

    assert captured == [(19, {0: {0: 1, 1: bytes(32)}, 1: manifest})]


def test_cache_lookup_duration_is_recorded_when_no_cache_exists(tmp_path: Path) -> None:
    client, _captured = client_with_capture()
    client.pending_bundle = ProjectBundle(tmp_path, 1, {})
    client.storage = StorageBackend(tmp_path)
    client.allow_compiled_cache_load = True
    recorded: list[str] = []
    client.record_host_duration = (  # type: ignore[method-assign]
        lambda field, _started: recorded.append(field)
    )
    client._submit_project = lambda _transfer: None  # type: ignore[method-assign]

    client._stage_persistent_cache_or_source()

    assert recorded == ["cache_read_ms", "source_materialize_ms"]


def test_background_cache_status_names_the_cache_and_keeps_gameplay_available(
    tmp_path: Path,
) -> None:
    client, captured = client_with_capture()
    client.storage = StorageBackend(tmp_path)
    client.pending_cache_after = None
    client.cache_ready = True

    client._refresh_compiled_cache("background")

    assert captured == [(60, {0: 2, 1: 0})]
    assert client.events.get_nowait() == FrontendEvent(
        "status",
        "正在后台生成项目缓存，可继续游戏，但游戏运行和响应速度可能暂时受到影响…",
    )


def test_full_project_chunks_stream_to_an_atomic_target(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    target = tmp_path / "full.reraproj"
    stream = AtomicExportStream.open(target)
    client.full_project_export = FullProjectExport(target, stream)
    descriptor = {0: 7, 1: 3, 2: 6, 3: blake3.blake3(b"abcdef").digest()}
    client.pending_export = _PendingExport(target, ExportStage.PROJECT_FILE, stream)

    client._handle_export_ready({0: 3, 1: variant(0, descriptor)})

    client._handle_export_chunk({0: 7, 1: 0, 2: b"abc", 3: False})
    client._handle_export_chunk({0: 7, 1: 3, 2: b"def", 3: True})

    assert target.read_bytes() == b"abcdef"
    assert client.full_project_export is None
    assert captured == [
        (67, {0: 7, 1: 0, 2: 16 * 1024 * 1024}),
        (67, {0: 7, 1: 3, 2: 16 * 1024 * 1024}),
    ]
    events = [client.events.get_nowait() for _ in range(client.events.qsize())]
    assert FrontendEvent("project_file_export_finished", True) in events


def test_display_line_service_returns_the_tui_projection() -> None:
    client, captured = client_with_capture()
    context = {0: 7, 1: 2, 2: 3}
    client._handle_service(
        {0: 9, 1: 10, 2: "get_display_line", 4: encode({0: context, 1: 0})}, None
    )
    assert ready_payload(captured) == {0: context, 1: "你好 RustyEra"}


def test_html_printed_str_service_uses_newest_first_logical_lines() -> None:
    client, captured = client_with_capture()
    client.presentation.lines.append(ServiceLine(2, True, 0, "A&B"))
    context = {0: 7, 1: 2, 2: 3}

    client._handle_service(
        {
            0: 10,
            1: 10,
            2: "html_get_printed_str",
            4: encode({0: context, 1: 0}),
        },
        None,
    )

    assert ready_payload(captured) == {
        0: context,
        1: "<p align='left'><nobr>A&amp;B</nobr></p>",
    }


def test_font_metrics_service_uses_terminal_cell_width() -> None:
    client, captured = client_with_capture()
    context = {0: 7, 1: 2, 2: 3}
    client._handle_service(
        {
            0: 10,
            1: 0,
            2: "gget_text_size",
            4: encode({0: context, 1: "A界", 2: "", 3: 12, 4: 0}),
        },
        None,
    )
    assert ready_payload(captured) == {0: context, 1: 3, 2: 1}


def test_image_metadata_service_decodes_submitted_webp_resource(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    image = b"RIFF\x16\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00\xe7\x03\x00\x64\x04\x00"
    digest = blake3.blake3(image).digest()
    client.pending_bundle = ProjectBundle(
        tmp_path,
        1,
        {
            "resources/剧情肖像/Rorona.webp": ProjectFile(
                "resources/剧情肖像/Rorona.webp",
                FILE_RESOURCE,
                variant(1, image),
                digest,
            )
        },
    )
    client.bundle = None
    client.reload_candidate = None

    client._handle_service(
        {
            0: 11,
            1: 1,
            2: "image_metadata",
            4: encode({0: "resources/剧情肖像/Rorona.webp", 1: digest}),
        },
        None,
    )

    assert ready_payload(captured) == {0: 1000, 1: 1125, 2: "webp", 3: False}


def test_image_metadata_service_lazily_reads_quick_scanned_resource(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    resources = tmp_path / "resources"
    resources.mkdir()
    image = b"RIFF\x16\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00\xe7\x03\x00\x64\x04\x00"
    image_path = resources / "Rorona.webp"
    image_path.write_bytes(image)
    digest = blake3.blake3(image).digest()
    ProjectBundle.scan_quick(tmp_path)
    client.pending_bundle = ProjectBundle.scan_quick(tmp_path)
    assert client.pending_bundle.files["resources/Rorona.webp"].payload == variant(
        3,
        {0: len(image), 1: {0: 1000, 1: 1125, 2: "webp", 3: False}},
    )
    client.bundle = None
    client.reload_candidate = None

    client._handle_service(
        {
            0: 11,
            1: 1,
            2: "image_metadata",
            4: encode({0: "resources/Rorona.webp", 1: digest}),
        },
        None,
    )

    assert ready_payload(captured) == {0: 1000, 1: 1125, 2: "webp", 3: False}


def test_client_hello_negotiates_the_pending_project_envelope_size(tmp_path: Path) -> None:
    client = object.__new__(RuntimeClient)
    client.pending_bundle = ProjectBundle(
        tmp_path,
        1,
        {
            "large.erb": ProjectFile(
                "large.erb",
                2,
                None,
                b"\x00" * 32,
                200 * 1024 * 1024,
            )
        },
    )
    captured: list[tuple[int, Any]] = []
    client.send_runtime = (  # type: ignore[method-assign]
        lambda tag, value, **_kwargs: captured.append((tag, value)) or 1
    )

    client._send_hello()

    assert captured[0][0] == 0
    limits = captured[0][1][3]
    assert limits[1] >= 200 * 1024 * 1024
    assert limits[0] > limits[1]
    assert limits[5] == 1024 * 1024 * 1024
    capabilities = captured[0][1][4]
    services = {(item[0], item[1]) for item in capabilities[10]}
    assert (7, "device_pump") in services
    assert (11, "rustyera.sql") in services
    assert (7, "get_key_state") not in services
    assert {item[0] for item in capabilities[12]} == {
        "input.timed_viewport",
        "input.device_pump",
    }


def test_same_batch_sql_cancellation_wins_before_provider_execution() -> None:
    client, captured = client_with_capture()
    client._pending_sql_requests = {}
    client._handle_service(
        {
            0: 41,
            1: 11,
            2: "rustyera.sql",
            3: {0: 1, 1: 0},
            4: b"not-executed",
            5: None,
        },
        73,
    )

    client._cancel_external_request({0: 41, 1: 1})

    assert client._pending_sql_requests == {}
    assert captured == []


def test_pump_drains_same_batch_sql_cancel_before_provider_execution(tmp_path: Path) -> None:
    submissions: list[bytes] = []
    session = {0: 1, 1: 2}
    service = {
        0: 41,
        1: 11,
        2: "rustyera.sql",
        3: {0: 1, 1: 0},
        4: b"not-executed",
        5: None,
    }
    pending = [
        encode_envelope(
            channel=CHANNEL_RUNTIME,
            channel_version=RUNTIME_VERSION,
            session=session,
            sequence=0,
            message_id=1,
            correlation_id=None,
            payload_tag=52,
            payload=runtime_message(52, service),
            epoch=7,
        ),
        encode_envelope(
            channel=CHANNEL_RUNTIME,
            channel_version=RUNTIME_VERSION,
            session=session,
            sequence=1,
            message_id=2,
            correlation_id=None,
            payload_tag=54,
            payload=runtime_message(54, {0: 41, 1: 1}),
            epoch=7,
        ),
    ]

    class FakeAbi:
        def submit(self, data: bytes) -> None:
            submissions.append(data)

        @staticmethod
        def drive() -> SimpleNamespace:
            return SimpleNamespace(state=0)

        @staticmethod
        def poll() -> bytes:
            return pending.pop(0) if pending else b""

    client = RuntimeClient(FakeAbi(), queue.Queue())  # type: ignore[arg-type]
    submissions.clear()
    bundle = ProjectBundle.scan(tmp_path)
    client.bundle = bundle
    client.storage = StorageBackend(tmp_path, resource_bundle=bundle)
    client.session = session
    client.epoch = 7
    client.expected_runtime_output = 0
    provider_calls: list[bytes] = []
    client.sql_provider = SimpleNamespace(
        handle=lambda payload, *_args: provider_calls.append(payload) or b"",
        reset=lambda: None,
    )

    assert client.pump()

    assert provider_calls == []
    assert client._pending_sql_requests == {}
    assert [decode_envelope(item).payload_tag for item in submissions] == [93]


def test_device_pump_waits_for_a_frontend_loop_acknowledgement() -> None:
    client, captured = client_with_capture()

    client._handle_service(
        {
            0: 17,
            1: 7,
            2: "device_pump",
            4: encode({0: 4, 1: 12}),
        },
        99,
    )

    assert captured == []
    assert client.events.get_nowait() == FrontendEvent("device_pump", 17)
    client.complete_device_pump(17)
    assert ready_payload(captured) == {0: 4, 1: 12}
    assert 17 not in client._pending_device_pumps


def test_runtime_output_responses_follow_the_batch_acknowledgement() -> None:
    submissions: list[bytes] = []

    class FakeAbi:
        def submit(self, data: bytes) -> None:
            submissions.append(data)

    client = RuntimeClient(FakeAbi(), queue.Queue())  # type: ignore[arg-type]
    submissions.clear()  # Ignore ClientHello.
    client.session = {0: 1, 1: 2}
    client.epoch = 4
    client._runtime_output_batch_active = True
    transition = client.send_runtime(20, {0: 0})
    after_transition = client.send_runtime(60, {0: 2, 1: 0})
    transition_service_result = client.send_runtime(
        53,
        {0: 17, 1: variant(0, b"exact revision")},
    )
    client._runtime_output_batch_active = False

    acknowledgement = client.send_runtime(93, {0: 7})
    client._flush_deferred_runtime_messages()

    envelopes = [decode_envelope(data) for data in submissions]
    assert [envelope.payload_tag for envelope in envelopes] == [93, 20, 53]
    assert [envelope.message_id for envelope in envelopes] == [
        acknowledgement,
        transition,
        transition_service_result,
    ]
    assert [envelope.epoch for envelope in envelopes] == [4, 4, 4]
    assert [envelope.sequence for envelope in envelopes] == [1, 2, 3]
    assert client._deferred_runtime_messages == [(after_transition, 60, {0: 2, 1: 0}, None)]

    client._handle_runtime = lambda *_args: None  # type: ignore[method-assign]
    client._handle_envelope(
        encode_envelope(
            channel=CHANNEL_RUNTIME,
            channel_version=RUNTIME_VERSION,
            session=client.session,
            sequence=0,
            message_id=8,
            correlation_id=transition,
            payload_tag=42,
            payload=runtime_message(42, {}),
            epoch=5,
        )
    )
    client._flush_deferred_runtime_messages()

    envelopes = [decode_envelope(data) for data in submissions]
    assert [envelope.payload_tag for envelope in envelopes] == [93, 20, 53, 60]
    assert envelopes[-1].message_id == after_transition
    assert envelopes[-1].epoch == 5
    assert envelopes[-1].sequence == 4


def test_projection_is_bound_to_the_revision_the_tui_rendered() -> None:
    client, captured = client_with_capture()

    client.projection(80, 24, 1, 6)
    assert captured == []

    client.projection(80, 24, 2, 7)
    assert captured == [
        (
            35,
            {
                0: 2,
                1: 7,
                2: {0: 80, 1: 24},
                3: 2,
                4: 80,
                5: "",
                6: {0: 1, 1: 1000, 2: 1, 3: 1000, 4: 0, 5: 0},
            },
        )
    ]


def test_stale_projection_rejection_is_recoverable_but_runtime_fault_is_structured() -> None:
    client, _captured = client_with_capture()
    client._projection_messages.add(9)
    client._handle_runtime(
        95,
        {0: 2, 1: "projection observation does not match the canonical presentation"},
        9,
    )
    assert client.events.empty()

    client._projection_messages.add(10)
    client._handle_runtime(95, {0: 3, 1: "projection dimensions must be positive"}, 10)
    invalid = client.events.get_nowait()
    assert invalid.kind == "runtime_error"

    client._handle_runtime(
        92,
        {
            0: 3,
            1: 'InvalidState("place storage is unavailable")',
            2: {
                0: "CallNative",
                1: "EVENTTRAIN",
                4: {0: "BEFORETRAIN.ERB", 3: 28},
            },
        },
        None,
    )
    fault = client.events.get_nowait()
    assert fault.kind == "runtime_fault"
    assert isinstance(fault.value, RuntimeFailure)
    assert fault.value.function == "EVENTTRAIN"
    assert fault.value.source_line == 28
