from __future__ import annotations

import queue
from typing import Any

from rustyera_tui.presentation import ServicePresentationModel
from rustyera_tui.runtime import RuntimeClient, RuntimeFailure
from rustyera_tui.wire import decode, encode, unwrap_variant


def client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any]]]:
    client = object.__new__(RuntimeClient)
    client.presentation = ServicePresentationModel(
        revision=7,
        lines=[{0: 1, 5: [[0, ["你好 RustyEra", None, None]]]}],
    )
    client.events = queue.Queue()
    client.session = {0: 1, 1: 2}
    client._projection_messages = set()
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
