from __future__ import annotations

from typing import Any

from rustyera_tui.presentation import DisplayLineModel, DisplaySegment, PresentationModel
from rustyera_tui.runtime import RuntimeClient
from rustyera_tui.wire import decode, encode, unwrap_variant


def client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any]]]:
    client = object.__new__(RuntimeClient)
    client.presentation = PresentationModel(
        revision=7,
        lines=[
            DisplayLineModel(
                line_id=1,
                temporary=False,
                logical_line_start=True,
                line_end=True,
                alignment=0,
                segments=(DisplaySegment("你好 RustyEra"),),
            )
        ],
    )
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
