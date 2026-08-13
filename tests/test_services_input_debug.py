from __future__ import annotations

from services_test_support import (
    Any,
    FrontendEvent,
    Path,
    PendingGameInput,
    SimpleNamespace,
    client_with_capture,
    debug_client_with_capture,
    unwrap_variant,
    variant,
)


def test_debug_variable_read_uses_requested_indices() -> None:
    client, captured = debug_client_with_capture()
    client.stop_token = {2: 11}
    descriptor = {0: b"key", 1: "FLAG", 2: 0, 3: 0, 4: [100], 5: True}

    client.request_debug_action("read_variable", (descriptor, (17,)))

    command_tag, fields = unwrap_variant(captured[0][1][1])
    assert captured[0][2] == "variable_value"
    assert command_tag == 11
    assert fields[1][6] == [17]


def test_diagnosis_export_blocks_input_undo_and_deadline_advancement(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    client.active_wait = {0: 7, 1: 3, 8: 1, 11: {0: 2, 1: 3}}

    client.export_diagnosis(tmp_path / "diagnosis.tar.zst", "fault log\n", "eraThe World")
    captured.clear()
    client.submit_text("blocked")
    client.activate({0: 2, 1: 4})
    client.input_undo({0: 2, 1: 3})
    client._advance_deadline()

    assert captured == []


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
