"""Wire fixtures copied from eraFL QUEST_MENU's UIC relative-division usage."""

from __future__ import annotations

from typing import Any

from rustyera_tui.wire import variant

GUILD_TASK_TOKEN = {0: 44, 1: 100}


def html_length(value: int, *, pixels: bool = False) -> list[object]:
    return variant(0 if pixels else 1, value)


def html_text(value: str) -> list[object]:
    return variant(0, value, 0, len(value))


def html_button(
    children: list[object],
    token: dict[int, int],
    title: str,
    *,
    enabled: bool = True,
) -> list[object]:
    return variant(
        1,
        7,
        [],
        children,
        {0: token[0], 1: token[1], 4: 3, 5: enabled},
        0,
        0,
        variant(4, "1", title, None),
    )


def html_shape(
    kind: str,
    parameters: list[object],
    *,
    color: int | None = None,
    button_color: int | None = None,
) -> list[object]:
    return variant(
        1,
        11,
        [],
        [],
        None,
        0,
        0,
        variant(8, kind, parameters, color, button_color),
    )


def html_image(source: str = "portrait.png") -> list[object]:
    return variant(
        1,
        10,
        [],
        [],
        None,
        0,
        0,
        variant(7, source, None, None, None, None, None),
    )


def html_division(
    x: int,
    y: int,
    width: int,
    height: int,
    children: list[object],
    *,
    margin: tuple[int, int, int, int] = (0, 0, 0, 0),
    padding: tuple[int, int, int, int] = (0, 0, 0, 0),
    border: tuple[int, int, int, int] = (0, 0, 0, 0),
    border_colors: tuple[int, int, int, int] = (0xC0C0C0,) * 4,
    depth: int = 0,
    background: int | None = None,
) -> list[object]:
    box_model: dict[int, object] = {}
    if any(margin):
        box_model[2] = [html_length(value) for value in margin]
    if any(padding):
        box_model[3] = [html_length(value) for value in padding]
    if any(border):
        box_model[0] = [html_length(value, pixels=True) for value in border]
        box_model[4] = list(border_colors)
    return variant(
        1,
        12,
        [],
        children,
        None,
        0,
        0,
        variant(
            9,
            html_length(x),
            html_length(y),
            html_length(width),
            html_length(height),
            depth,
            background,
            True,
            box_model,
        ),
    )


def erafl_guild_document() -> dict[int, Any]:
    """Return the nine divisions emitted by QUEST_MENU.QST_MENU_SHOW_TABLE."""

    all_sides_25 = (25, 25, 25, 25)
    all_sides_100 = (100, 100, 100, 100)
    one_pixel = (1, 1, 1, 1)
    return {
        0: [
            html_division(50, 0, 8_600, 4_500, []),
            html_division(
                50,
                0,
                8_600,
                200,
                [html_text("◆　公会")],
                border=one_pixel,
                padding=all_sides_25,
            ),
            html_division(
                50,
                200,
                8_600,
                200,
                [html_text("[任务板]")],
                border=one_pixel,
                padding=all_sides_25,
            ),
            html_division(
                50,
                200,
                8_600,
                200,
                [html_text(" " * 150 + "所持金:$10,000")],
                padding=all_sides_25,
            ),
            html_division(50, 400, 8_600, 4_100, [], border=one_pixel),
            html_division(
                50,
                400,
                3_300,
                4_000,
                [
                    html_text("◇可以接取的任务◇\n"),
                    html_button(
                        [html_text("女仆班米爱尔")],
                        GUILD_TASK_TOKEN,
                        "选择任务",
                    ),
                ],
                margin=all_sides_100,
                padding=all_sides_25,
                border=one_pixel,
            ),
            html_division(
                3_250,
                400,
                5_400,
                2_600,
                [
                    html_text("任务详情"),
                    html_division(
                        100,
                        200,
                        1_000,
                        400,
                        [html_text("嵌套区")],
                        border=one_pixel,
                    ),
                ],
                margin=all_sides_100,
                padding=all_sides_25,
                border=one_pixel,
            ),
            html_division(
                3_250,
                3_000,
                5_400,
                1_300,
                [html_text("队伍编成")],
                margin=(0, 100, 0, 100),
                padding=all_sides_25,
                border=one_pixel,
            ),
            html_division(
                50,
                4_300,
                8_600,
                200,
                [html_text("[取消]")],
                padding=all_sides_25,
            ),
        ]
    }


def erafl_guild_line() -> dict[int, object]:
    return {
        0: 12,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [variant(2, erafl_guild_document())],
    }
