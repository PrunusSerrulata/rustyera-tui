from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import blake3

from rustyera_tui.configuration import ConfigurationChange, ConfigurationSnapshot
from rustyera_tui.project import FILE_RESOURCE, ProjectBundle, ProjectFile
from rustyera_tui.presentation import ServicePresentationModel
from rustyera_tui.runtime import (
    AtomicExportStream,
    FrontendEvent,
    FullProjectExport,
    PendingConfigurationPrepare,
    PendingGameInput,
    RuntimeClient,
    RuntimeFailure,
)
from rustyera_tui.wire import decode, encode, unwrap_variant, variant


def client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any]]]:
    client = object.__new__(RuntimeClient)
    client.presentation = ServicePresentationModel(
        revision=7,
        lines=[{0: 1, 5: [[0, ["你好 RustyEra", None, None]]]}],
    )
    client.events = queue.Queue()
    client.session = {0: 1, 1: 2}
    client.active_wait = None
    client._projection_messages = set()
    client._input_messages = {}
    client.pending_cache_export_message = None
    client.pending_export_message = None
    client.pending_export_kind = None
    client.pending_export = None
    client.pending_cache_stream = None
    client.full_project_export = None
    client.cache_preparation_started = False
    client.cache_refresh_pending = False
    client.cache_refresh_after_ns = 0
    client.pending_diagnosis = None
    client.pending_start_after_configuration = None
    client.pending_restore = None
    client.new_game_seed = None
    client.configuration_profile_supported = True
    client.abi = SimpleNamespace(
        supports_project_configuration_updates=True,
        prepare_project_configuration_update=lambda *_args: (0, b""),
        project_file_manifest=lambda _bytes: {0: 1, 1: []},
    )
    client.single_step_enabled = False
    captured: list[tuple[int, Any]] = []
    client.send_runtime = (  # type: ignore[method-assign]
        lambda tag, value, **_kwargs: captured.append((tag, value)) or 1
    )
    return client, captured


def test_full_project_export_preempts_cache_and_cleans_up_on_cancel(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    manifest = {0: 1, 1: []}
    client.bundle = SimpleNamespace(
        project_file=None,
        materialize=lambda _progress, _cancelled: SimpleNamespace(manifest=lambda: manifest),
    )
    client.pending_export_kind = 2
    client.cache_preparation_started = True
    client.pending_cache_stream = AtomicExportStream.open(tmp_path / "cache.reracache")
    target = tmp_path / "full.reraproj"

    client.export_project_file(target, lambda: False)

    assert [tag for tag, _value in captured] == [71, 70, 60]
    assert captured[0][1] == {0: 2}
    assert captured[1][1] == {0: manifest}
    assert client.full_project_export is not None
    temporary = client.full_project_export.stream.temporary
    assert temporary.exists()
    assert client.cache_refresh_pending

    client.cancel_project_file_export()

    assert captured[-1] == (71, {0: 3})
    assert client.full_project_export is None
    assert not temporary.exists()
    events = [client.events.get_nowait() for _ in range(client.events.qsize())]
    assert FrontendEvent("project_file_export_finished", None) in events
    assert FrontendEvent("project_progress_finished") in events


def test_full_project_chunks_stream_to_an_atomic_target(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    target = tmp_path / "full.reraproj"
    stream = AtomicExportStream.open(target)
    client.full_project_export = FullProjectExport(target, stream)
    client.pending_export_kind = 5
    descriptor = {0: 7, 1: 3, 2: 6, 3: blake3.blake3(b"abcdef").digest()}
    client.pending_export = (target, bytearray(), descriptor)

    client._handle_export_chunk({0: 7, 1: 0, 2: b"abc", 3: False})
    client._handle_export_chunk({0: 7, 1: 3, 2: b"def", 3: True})

    assert target.read_bytes() == b"abcdef"
    assert client.full_project_export is None
    assert captured[-1] == (67, {0: 7, 1: 3, 2: 1024 * 1024})
    events = [client.events.get_nowait() for _ in range(client.events.qsize())]
    assert FrontendEvent("project_file_export_finished", True) in events


def ready_payload(captured: list[tuple[int, Any]]) -> dict[int, Any]:
    assert captured[0][0] == 53
    result_tag, result_fields = unwrap_variant(captured[0][1][1])
    assert result_tag == 0
    return decode(result_fields[0])


def debug_client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any, str]]]:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.phase = 4
    client.active_wait = None
    client.debug_requested = True
    client.debug_grant = {1: {0: {0: 7, 1: 9}}}
    client.stop_token = None
    client.selected_fiber = None
    client.pending_debug_actions = []
    client.debug_pending_by_message = {}
    client.single_step_enabled = False
    client.debug_step_in_flight = False
    client.debug_disable_pending = False
    client.transient_pause_owner = None
    client.transient_close_pending = None
    captured: list[tuple[int, Any, str]] = []
    client.send_debug = (  # type: ignore[method-assign]
        lambda tag, value, pending="": captured.append((tag, value, pending)) or 1
    )
    return client, captured


def test_debug_variable_read_uses_requested_indices() -> None:
    client, captured = debug_client_with_capture()
    client.stop_token = {2: 11}
    descriptor = {0: b"key", 1: "FLAG", 2: 0, 3: 0, 4: [100], 5: True}

    client.request_debug_action("read_variable", (descriptor, (17,)))

    command_tag, fields = unwrap_variant(captured[0][1][1])
    assert captured[0][2] == "variable_value"
    assert command_tag == 11
    assert fields[1][6] == [17]


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
    assert client.pending_bundle.files["resources/Rorona.webp"].payload is None
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


def test_snapshot_export_purposes_and_restore_warnings_are_frontend_visible(
    tmp_path: Path,
) -> None:
    client, captured = client_with_capture()
    client.pending_export = None
    client.pending_export_kind = None
    client.pending_export_message = None
    client.pending_diagnosis = None
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")

    client.export_snapshot(tmp_path / "debug.snapshot", "debug")
    assert captured.pop() == (60, {0: 1, 1: 1})

    client.pending_export = None
    client.pending_export_kind = None
    client.pending_export_message = None
    client.export_diagnosis(
        tmp_path / "diagnosis.tar.zst",
        "complete log\n",
        "eraThe World",
    )
    assert captured.pop() == (60, {0: 1, 1: 2})

    client._handle_runtime(
        97,
        {
            0: "runtime.snapshot_restored_from_diagnosis",
            1: 2,
            2: "restored a VM snapshot captured for diagnosis",
        },
        None,
    )
    assert client.events.get_nowait().kind == "log"
    warning = client.events.get_nowait()
    assert warning.kind == "snapshot_restore_warning"
    assert "诊断信息" in warning.value


def test_diagnosis_export_waits_for_an_existing_state_transfer(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    client.pending_export = (tmp_path / "compiled.bin", bytearray(), None)
    client.pending_export_kind = 2

    client.export_diagnosis(
        tmp_path / "diagnosis.tar.zst",
        "fault log\n",
        "eraThe World",
    )

    assert captured == []
    assert client.pending_diagnosis is not None
    assert client.pending_diagnosis.stage == "export_wait"

    client.pending_export = None
    client.maybe_refresh_compiled_cache()

    assert captured == [(60, {0: 1, 1: 2})]
    assert client.pending_export_kind == 3
    assert client.pending_diagnosis.stage == "snapshot"


def test_rejected_input_reports_the_still_active_wait_to_the_app() -> None:
    client, _captured = client_with_capture()
    wait = {0: 7, 1: 6, 11: {0: 1, 1: 2}}
    client.active_wait = wait
    client._input_messages[23] = PendingGameInput(wait, variant(2, "bad"), False)

    client._handle_runtime(
        95,
        {0: 3, 1: "input value does not match the active wait"},
        23,
    )

    rejected = client.events.get_nowait()
    assert rejected == FrontendEvent("interaction_rejected", wait)
    error = client.events.get_nowait()
    assert error.kind == "runtime_error"
    assert 23 not in client._input_messages


def test_one_correlated_stale_input_retries_on_the_next_wait_of_the_same_kind() -> None:
    client, captured = client_with_capture()
    previous = {0: 7, 1: 2, 11: {0: 1, 1: 2}}
    active = {0: 8, 1: 2, 11: {0: 1, 1: 3}}
    client.active_wait = active
    client._input_messages[23] = PendingGameInput(previous, variant(2, "412"), False)

    client._handle_runtime(95, {0: 2, 1: "input wait identity is stale"}, 23)

    assert captured == [
        (
            30,
            {
                0: 8,
                1: {0: 1, 1: 3},
                2: captured[0][1][2],
                3: variant(2, "412"),
                4: False,
            },
        )
    ]
    assert client.events.empty()
    retried = next(iter(client._input_messages.values()))
    assert retried.wait == active
    assert retried.stale_retries == 1


def test_fiber_pages_use_the_requested_cursor_and_protocol_maximum() -> None:
    client, captured = debug_client_with_capture()
    stop = {0: 1, 1: 2, 2: 3, 3: 4}
    client.stop_token = stop

    client._run_debug_action("fibers", 1024)

    assert captured == [
        (
            10,
            {0: client.debug_grant[1], 1: variant(30, stop, 1024, 1024)},
            "fibers",
        )
    ]


def test_single_step_host_wait_auto_continues_before_gameplay_input() -> None:
    client, captured = debug_client_with_capture()
    client.single_step_enabled = True
    stop = {0: 1, 1: 2, 2: 3, 3: 4}

    client._handle_debug(
        12,
        {0: stop, 1: variant(3), 2: 6, 3: {0: "ERB/main.erb", 4: 12}},
        None,
    )

    event = client.events.get_nowait()
    assert event.kind == "debug_stopped"
    assert captured == [(10, {0: client.debug_grant[1], 1: variant(1, stop)}, "auto_continue")]


def test_console_owned_pause_resumes_with_the_refreshed_stop_after_close() -> None:
    client, captured = debug_client_with_capture()
    old_stop = {0: 1, 1: 2, 2: 3, 3: 4}
    new_stop = {0: 1, 1: 2, 2: 3, 3: 5}
    client.stop_token = old_stop
    client.transient_pause_owner = "console"
    client.debug_pending_by_message = {42: "console"}

    client.close_debug_surface("console")
    assert captured == []

    client._handle_debug(
        11,
        variant(8, {0: new_stop, 1: variant(0, 3), 2: [], 3: [], 4: [], 5: []}),
        42,
    )

    assert captured == [
        (10, {0: client.debug_grant[1], 1: variant(1, new_stop)}, "transient_continue")
    ]


def test_disabling_debug_revokes_the_grant_instead_of_leaving_a_paused_vm() -> None:
    client, captured = debug_client_with_capture()
    stop = {0: 1, 1: 2, 2: 3, 3: 4}
    client.stop_token = stop

    client.disable_debug()

    assert captured == [
        (10, {0: client.debug_grant[1], 1: variant(1, stop)}, "disable_continue"),
    ]
    assert client.debug_disable_pending
    assert client.events.empty()

    client.debug_pending_by_message = {41: "disable_continue"}
    client._handle_debug(11, variant(0), 41)

    assert captured == [
        (10, {0: {0: {0: 7, 1: 9}}, 1: variant(1, stop)}, "disable_continue"),
        (
            2,
            {0: {0: 7, 1: 9}, 1: "frontend disabled debugging"},
            "",
        ),
    ]
    assert client.debug_grant is None
    assert client.stop_token is None
    response = client.events.get_nowait()
    assert response.kind == "debug_response"
    event = client.events.get_nowait()
    assert event.kind == "debug_enabled"
    assert event.value is False


def test_debug_paused_client_does_not_submit_state_changing_gameplay_input() -> None:
    client, captured_debug = debug_client_with_capture()
    captured_runtime: list[tuple[int, Any]] = []
    client.phase = 7
    client.active_wait = {0: 4, 1: 0, 11: {0: 1, 1: 2}}
    client.send_runtime = (  # type: ignore[method-assign]
        lambda tag, value, **_kwargs: captured_runtime.append((tag, value)) or 1
    )

    client.submit_text("")
    client.activate({0: 1})

    assert captured_runtime == []
    assert captured_debug == []
    event = client.events.get_nowait()
    assert event.kind == "log"
    assert "暂不提交游戏输入" in event.value


def test_message_skip_accepts_enter_and_any_key_waits_once() -> None:
    client, captured = client_with_capture()
    first = {0: 10, 1: 0, 4: False, 11: {0: 1, 1: 10}}
    client._set_active_wait(first)

    client.skip_message_waits()
    client._set_active_wait(first)
    client._set_active_wait(None)
    client._set_active_wait({0: 11, 1: 0, 4: False, 11: {0: 1, 1: 11}})

    submissions = [value for tag, value in captured if tag == 30]
    assert [submission[0] for submission in submissions] == [10]
    assert all(submission[3] == [0, []] for submission in submissions)
    assert all(submission[4] for submission in submissions)

    captured.clear()
    client._set_active_wait({0: 12, 1: 1, 4: False, 11: {0: 1, 1: 12}})
    client.skip_message_waits()
    any_key = [value for tag, value in captured if tag == 30]
    assert len(any_key) == 1
    assert any_key[0][3] == [1, ["\n"]]
    assert any_key[0][4] is True


def test_configuration_update_uses_authoritative_snapshot_and_open_effect_is_supported() -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(project_file=None)
    client.pending_configuration = None
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 7, 1: b"digest", 2: [], 3: False}
    )

    client.prepare_configuration_update([ConfigurationChange("FontSize", "20")])
    assert captured.pop() == (
        24,
        {0: 7, 1: b"digest", 2: [{0: "FontSize", 1: "20"}]},
    )
    assert isinstance(client.pending_configuration, PendingConfigurationPrepare)
    assert client.pending_configuration.message_id == 1

    client._acknowledge_effects({0: [{0: 41, 1: variant(4)}]})
    assert client.events.get_nowait() == FrontendEvent("open_configuration")
    assert captured.pop() == (43, {0: [{0: 41, 1: 0}]})


def test_configuration_update_requires_the_negotiated_tui_profile() -> None:
    client, _captured = client_with_capture()
    client.bundle = SimpleNamespace(project_file=None)
    client.configuration_profile_supported = False
    client.pending_configuration = None
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 7, 1: b"digest", 2: [], 3: False}
    )

    try:
        client.prepare_configuration_update([])
    except RuntimeError as error:
        assert "不支持 TUI" in str(error)
    else:
        raise AssertionError("an unsupported configuration profile was accepted")


def test_packaged_configuration_commits_through_the_append_update(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"base")
    bundle = ProjectBundle(tmp_path, 7, {}, project_file)
    client.bundle = bundle
    client.pending_configuration = None
    digest = b"package-digest"
    hot_entry = {
        0: "UseMouse",
        1: "UseMouse",
        2: "UseMouse",
        3: "YES",
        4: 0,
        5: [],
        6: False,
        7: 2,
        8: "YES",
        9: "YES",
        10: 0,
    }
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 7, 1: digest, 2: [hot_entry], 3: False}
    )

    client.prepare_configuration_update([ConfigurationChange("UseMouse", "NO")])
    pending = client.pending_configuration
    assert isinstance(pending, PendingConfigurationPrepare)
    contents = "UseMouse:NO\n"
    prepared_digest = blake3.blake3(contents.encode()).digest()
    client.abi.prepare_project_configuration_update = lambda *_args: (4, b"journal")
    client.abi.project_file_manifest = lambda _bytes: {
        0: 7,
        1: [{0: "reraconfig.toml", 1: 5, 2: variant(0, contents), 3: prepared_digest}],
    }
    client._handle_configuration_prepared(
        {0: 7, 1: digest, 2: contents, 3: False, 4: prepared_digest}, pending.message_id
    )
    assert captured[-1] == (26, {0: pending.message_id, 1: 1})
    client._handle_configuration_committed(
        {
            0: {
                0: 7,
                1: prepared_digest,
                2: [{**hot_entry, 3: "NO", 9: "NO"}],
                3: False,
            }
        },
        1,
    )

    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert FrontendEvent("configuration_saved", (False, False)) in events
    assert project_file.read_bytes() == b"basejournal"
    assert client.bundle is not bundle
    assert client.bundle.identity() != bundle.identity()


def test_packaged_configuration_restarts_with_the_updated_identity(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"base")
    old_source = "[save]\nauto_save = true\n"
    old_digest = blake3.blake3(old_source.encode()).digest()
    bundle = ProjectBundle.from_project_file_manifest(
        project_file,
        {
            0: 8,
            1: [{0: "reraconfig.toml", 1: 5, 2: variant(0, old_source), 3: old_digest}],
        },
    )
    client.bundle = bundle
    client.pending_configuration = None
    hot_entry = {
        0: "UseMouse",
        1: "UseMouse",
        2: "UseMouse",
        3: "YES",
        4: 0,
        5: [],
        6: False,
        7: 2,
        8: "YES",
        9: "YES",
        10: 0,
    }
    restart_entry = {**hot_entry, 0: "AutoSave", 1: "AutoSave", 2: "AutoSave", 10: 1}
    fixed_entry = {**hot_entry, 0: "BackColor", 1: "BackColor", 2: "BackColor", 6: True}
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 8, 1: old_digest, 2: [hot_entry, restart_entry, fixed_entry], 3: False}
    )

    client.prepare_configuration_update([ConfigurationChange("AutoSave", "NO")], True)
    assert captured[0][0] == 24
    pending = client.pending_configuration
    assert isinstance(pending, PendingConfigurationPrepare)
    contents = "[save]\nauto_save = false\n"
    prepared_digest = blake3.blake3(contents.encode()).digest()
    client.abi.prepare_project_configuration_update = lambda *_args: (4, b"journal")
    client.abi.project_file_manifest = lambda _bytes: {
        0: 8,
        1: [{0: "reraconfig.toml", 1: 5, 2: variant(0, contents), 3: prepared_digest}],
    }
    recreated: list[tuple[ProjectBundle, bytes | None]] = []

    def recreate(candidate: ProjectBundle, **options: Any) -> None:
        recreated.append((candidate, options.get("project_file_bytes")))

    client.recreate = recreate  # type: ignore[method-assign]
    client._handle_configuration_prepared(
        {0: 8, 1: old_digest, 2: contents, 3: True, 4: prepared_digest}, pending.message_id
    )
    client._handle_configuration_committed(
        {0: {0: 8, 1: prepared_digest, 2: [restart_entry], 3: False}}, 1
    )

    assert len(recreated) == 1
    candidate, submitted_project = recreated[0]
    assert candidate.identity() != bundle.identity()
    assert submitted_project == b"basejournal"


def test_prepared_configuration_writes_and_restarts_without_exposing_wire_maps(
    tmp_path: Path,
) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[text]\nfont_size = 18\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None
    client, _captured = client_with_capture()
    client.bundle = bundle
    client.pending_configuration = PendingConfigurationPrepare(7, 1, digest, True)
    recreated: list[ProjectBundle] = []
    client.recreate = recreated.append  # type: ignore[method-assign]

    contents = "[text]\nfont_size = 20\n"
    prepared_digest = blake3.blake3(contents.encode()).digest()
    client._handle_configuration_prepared(
        {0: 1, 1: digest, 2: contents, 3: False, 4: prepared_digest}, 7
    )

    assert config.read_text(encoding="utf-8") == "[text]\nfont_size = 20\n"
    client._handle_configuration_committed({0: {0: 1, 1: prepared_digest, 2: [], 3: True}}, 1)
    assert len(recreated) == 1
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert FrontendEvent("configuration_saved", (True, True)) in events


def test_generated_reraconfig_is_persisted_idempotently(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.bundle = ProjectBundle.scan(tmp_path)
    client.configuration_snapshot = None
    client.pending_configuration = None
    generated = "[meta]\nschema_version = 2\n"
    wire = {0: 1, 1: b"", 2: [], 3: False, 4: generated}

    assert client._publish_configuration(wire) is not None
    assert client._publish_configuration(wire) is not None
    assert (tmp_path / "reraconfig.toml").read_text(encoding="utf-8") == generated
    assert [command for command in captured if command[0] == 24] == [(24, {0: 1, 1: b"", 2: []})]


def test_upgraded_reraconfig_uses_the_original_source_digest(tmp_path: Path) -> None:
    original = "[meta]\nschema_version = 1\n[text]\nfont_size = 20\n"
    generated = "[meta]\nschema_version = 2\n[text]\nfont_size = 20\n"
    (tmp_path / "reraconfig.toml").write_text(original, encoding="utf-8")
    client, captured = client_with_capture()
    client.bundle = ProjectBundle.scan(tmp_path)
    client.pending_configuration = None
    wire = {
        0: 1,
        1: blake3.blake3(original.encode()).digest(),
        2: [],
        3: False,
        4: generated,
    }

    assert client._publish_configuration(wire) is not None
    assert (tmp_path / "reraconfig.toml").read_text(encoding="utf-8") == generated
    pending = client.pending_configuration
    assert isinstance(pending, PendingConfigurationPrepare)
    assert pending.automatic
    generated_digest = blake3.blake3(generated.encode()).digest()
    client._handle_configuration_prepared(
        {0: 1, 1: pending.source_digest, 2: generated, 3: False, 4: generated_digest},
        pending.message_id,
    )
    client.pending_start_after_configuration = False
    client._handle_configuration_committed(
        {0: {0: 1, 1: generated_digest, 2: [], 3: False, 4: None}},
        1,
    )
    assert captured[-1] == (20, {0: variant(0, None)})

    client.prepare_configuration_update([ConfigurationChange("FontSize", "22")])
    assert captured[-1] == (
        24,
        {0: 1, 1: generated_digest, 2: [{0: "FontSize", 1: "22"}]},
    )


def test_invalid_or_conflicting_prepared_configuration_keeps_session_alive(
    tmp_path: Path,
) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[text]\nfont_size = 18\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None
    client, _captured = client_with_capture()
    client.bundle = bundle
    client.pending_configuration = PendingConfigurationPrepare(7, 1, digest, False)

    client._handle_configuration_prepared({0: "bad"}, 7)
    failure = client.events.get_nowait()
    assert failure.kind == "configuration_save_failed"
    assert "保存偏好选项失败" in failure.value
    assert config.read_text(encoding="utf-8") == "[text]\nfont_size = 18\n"
    client._handle_configuration_committed({0: {0: 1, 1: digest, 2: [], 3: False}}, 1)
    assert client.pending_configuration is None
    assert client.events.get_nowait().kind == "configuration"

    config.write_text("[text]\nfont_size = 19\n", encoding="utf-8")
    client.pending_configuration = PendingConfigurationPrepare(8, 1, digest, False)
    contents = "[text]\nfont_size = 20\n"
    client._handle_configuration_prepared(
        {
            0: 1,
            1: digest,
            2: contents,
            3: True,
            4: blake3.blake3(contents.encode()).digest(),
        },
        8,
    )
    conflict = client.events.get_nowait()
    assert conflict.kind == "configuration_save_failed"
    assert "其他程序修改" in conflict.value
    assert config.read_text(encoding="utf-8") == "[text]\nfont_size = 19\n"


def test_invalid_committed_configuration_stops_the_success_path(tmp_path: Path) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[interaction]\nuse_mouse = true\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None
    client, _captured = client_with_capture()
    client.bundle = bundle
    client.pending_configuration = PendingConfigurationPrepare(7, 1, digest, True)
    recreated: list[ProjectBundle] = []
    client.recreate = recreated.append  # type: ignore[method-assign]
    contents = "[interaction]\nuse_mouse = false\n"
    client._handle_configuration_prepared(
        {
            0: 1,
            1: digest,
            2: contents,
            3: False,
            4: blake3.blake3(contents.encode()).digest(),
        },
        7,
    )

    client._handle_configuration_committed({0: {0: "bad"}}, 1)

    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert any(event.kind == "configuration_save_failed" for event in events)
    assert not any(event.kind == "configuration_saved" for event in events)
    assert not recreated
