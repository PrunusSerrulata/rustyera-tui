from __future__ import annotations

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
    SimpleNamespace,
    StorageBackend,
    blake3,
    client_with_capture,
    encode,
    ready_payload,
    variant,
)


def test_server_hello_reports_the_runtime_product_version() -> None:
    client, _ = client_with_capture()
    client.pending_bundle = None

    client._handle_runtime(1, {1: {0: 1, 1: 2}, 4: 7, 7: 1, 8: "0.6.0"}, None)

    assert client.events.get_nowait() == FrontendEvent("runtime_version", "0.6.0")


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
    client.pending_export_kind = 5
    descriptor = {0: 7, 1: 3, 2: 6, 3: blake3.blake3(b"abcdef").digest()}
    client.pending_export = (target, bytearray(), None)

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
    client.presentation.lines.append({0: 2, 2: True, 4: 0, 5: [[0, ["A&B", None, None]]]})
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
