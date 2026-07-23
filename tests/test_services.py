from __future__ import annotations

import queue
from typing import Any

from rustyera_tui.presentation import ServicePresentationModel
from rustyera_tui.runtime import RuntimeClient, RuntimeFailure
from rustyera_tui.wire import decode, encode, unwrap_variant, variant


def client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any]]]:
    client = object.__new__(RuntimeClient)
    client.presentation = ServicePresentationModel(
        revision=7,
        lines=[{0: 1, 5: [[0, ["你好 RustyEra", None, None]]]}],
    )
    client.events = queue.Queue()
    client.session = {0: 1, 1: 2}
    client._projection_messages = set()
    client._message_skip_active = False
    client._message_skip_wait_id = None
    client.single_step_enabled = False
    captured: list[tuple[int, Any]] = []
    client.send_runtime = (  # type: ignore[method-assign]
        lambda tag, value, **_kwargs: captured.append((tag, value)) or 1
    )
    return client, captured


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


def test_display_line_service_returns_the_tui_projection() -> None:
    client, captured = client_with_capture()
    context = {0: 7, 1: 2, 2: 3}
    client._handle_service(
        {0: 9, 1: 10, 2: "get_display_line", 4: encode({0: context, 1: 0})}, None
    )
    assert ready_payload(captured) == {0: context, 1: "你好 RustyEra"}


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
    assert invalid.kind == "error"

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
        )
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


def test_message_skip_submits_each_enter_wait_once_and_stops_at_forcewait() -> None:
    client, captured = client_with_capture()
    first = {0: 10, 1: 0, 4: False, 11: {0: 1, 1: 10}}
    second = {0: 11, 1: 0, 4: False, 11: {0: 1, 1: 11}}
    force = {0: 12, 1: 0, 4: True, 11: {0: 1, 1: 12}}
    client._set_active_wait(first)

    client.skip_enter_waits()
    client._set_active_wait(first)
    client._set_active_wait(None)
    client._set_active_wait(second)
    client._set_active_wait(force)

    submissions = [value for tag, value in captured if tag == 30]
    assert [submission[0] for submission in submissions] == [10, 11]
    assert all(submission[3] == [0, []] for submission in submissions)
    assert all(submission[4] for submission in submissions)
    assert not client._message_skip_active
