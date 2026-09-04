from rich.cells import cell_len

from erafl_layout_fixture import (
    GUILD_TASK_TOKEN,
    erafl_guild_document,
    erafl_guild_line,
    html_button,
    html_division,
    html_image,
    html_length,
    html_shape,
    html_text,
)
from presentation_test_support import color, style, terminal_column
from rustyera_tui.presentation import (
    DEFAULT_VIEWPORT_COLUMNS,
    SeparatorLayout,
    parse_line,
    plain_line,
)
from rustyera_tui.wire import variant


def test_erafl_guild_divisions_project_to_terminal_table_geometry() -> None:
    """Mirror QUEST_MENU's real UIC parent/left/right/party relative divisions."""

    assert len(erafl_guild_document()[0]) == 9
    parsed = parse_line(erafl_guild_line())
    rows = "".join(segment.text for segment in parsed.segments).splitlines()

    # Web trace: main=(8,28.59)-(1384,684.59), left=24..520,
    # right=536..1368. The 8x16 projection is therefore x=1..173,
    # left=3..65 and right=67..171, with 45 occupied terminal rows.
    assert len(rows) == 45
    assert max(cell_len(row) for row in rows) == 173
    assert rows[5][3] == "┌"
    assert rows[5][64] == "┐"
    assert rows[5][67] == "┌"
    assert rows[5][170] == "┐"
    assert rows[28][67] == "└"
    assert rows[28][170] == "┘"
    assert rows[30][67] == "┌"
    assert rows[30][170] == "┐"
    assert [index for index, character in enumerate(rows[29]) if character == "│"] == [
        1,
        3,
        64,
        172,
    ]
    left_row = next(index for index, row in enumerate(rows) if "◇可以接取的任务◇" in row)
    detail_row = next(index for index, row in enumerate(rows) if "任务详情" in row)
    party_row = next(index for index, row in enumerate(rows) if "队伍编成" in row)
    assert terminal_column(rows[left_row], "◇可以接取的任务◇") < terminal_column(
        rows[detail_row], "任务详情"
    )
    assert party_row > detail_row
    assert terminal_column(rows[party_row], "队伍编成") == terminal_column(
        rows[detail_row], "任务详情"
    )
    nested_row = next(index for index, row in enumerate(rows) if "嵌套区" in row)
    assert nested_row > detail_row
    assert terminal_column(rows[nested_row], "嵌套区") > terminal_column(
        rows[detail_row], "任务详情"
    )
    button = next(segment for segment in parsed.segments if segment.token == GUILD_TASK_TOKEN)
    assert button.text == "女仆班米爱尔"
    assert button.title == "选择任务"
    assert any(
        "┌" in segment.text and segment.style.foreground == "#c0c0c0" for segment in parsed.segments
    )


def test_positioned_html_keeps_graphemes_and_bounds_clipped_content() -> None:
    text = "界e\u0301👩‍💻尾"
    document = {
        0: [
            html_division(
                0,
                0,
                1_000,
                300,
                [
                    html_image(),
                    html_text(text),
                    html_division(5_000, 0, 500, 100, [html_text("OUTSIDE")]),
                ],
            )
        ]
    }
    parsed = parse_line({0: 16, 1: False, 2: True, 3: True, 4: 0, 5: [variant(2, document)]})

    visible = [segment for segment in parsed.segments if segment.text != "\n"]
    assert "".join(segment.text for segment in visible) == text
    assert sum(segment.logical_columns or cell_len(segment.text) for segment in visible) == 7
    assert "OUTSIDE" not in "".join(segment.text for segment in parsed.segments)


def test_positioned_html_depth_and_source_order_choose_the_top_layer() -> None:
    document = {
        0: [
            html_division(0, 0, 100, 100, [html_text("P")], depth=1),
            html_division(0, 0, 100, 100, [html_text("N")], depth=-1),
            html_division(500, 0, 100, 100, [html_text("A")]),
            html_division(500, 0, 100, 100, [html_text("B")]),
        ]
    }
    parsed = parse_line({0: 17, 1: False, 2: True, 3: True, 4: 0, 5: [variant(2, document)]})
    row = "".join(segment.text for segment in parsed.segments).splitlines()[0]

    assert row[0] == "P"
    assert row[10] == "B"


def test_button_wrapped_division_and_shape_preserve_interaction_context() -> None:
    token = {0: 77, 1: 12_345}
    shape = html_shape(
        "rect",
        [html_length(0), html_length(45), html_length(200), html_length(10)],
        color=0x112233,
        button_color=0xABCDEF,
    )
    button = html_button(
        [
            html_division(
                0,
                0,
                500,
                300,
                [shape],
                border=(1, 1, 1, 1),
                background=0x010203,
            )
        ],
        token,
        "不可用任务",
        enabled=False,
    )
    parsed = parse_line(
        {
            0: 18,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(2, {0: [button]})],
        }
    )

    interactive = [segment for segment in parsed.segments if segment.token == token]
    assert interactive
    assert all(not segment.enabled for segment in interactive)
    assert all(segment.title == "不可用任务" for segment in interactive)
    shape_segment = next(segment for segment in interactive if "━" in segment.text)
    assert shape_segment.style.foreground == "#112233"
    assert shape_segment.hover_style is not None
    assert shape_segment.hover_style.foreground == "#abcdef"
    assert any(segment.style.background == "#010203" for segment in interactive)


def test_overlapping_top_layer_owns_the_final_interaction_cells() -> None:
    token = {0: 91, 1: 92}
    button_layer = html_button(
        [html_division(0, 0, 400, 100, [html_text("BUTTON")])],
        token,
        "covered",
    )
    cover = html_division(0, 0, 400, 100, [html_text("TOP")], depth=1)
    parsed = parse_line(
        {
            0: 21,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(2, {0: [button_layer, cover]})],
        }
    )
    row_segments = [segment for segment in parsed.segments if segment.text != "\n"]

    assert "".join(segment.text for segment in row_segments).startswith("TOP")
    assert row_segments[0].token is None


def test_margin_collapse_and_single_sided_border_do_not_create_false_boxes() -> None:
    collapsed = html_division(
        0,
        0,
        100,
        100,
        [html_text("hidden")],
        margin=(100, 100, 100, 100),
    )
    top_only = html_division(
        0,
        200,
        200,
        100,
        [],
        border=(1, 0, 0, 0),
    )
    parsed = parse_line(
        {
            0: 19,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(2, {0: [collapsed, top_only]})],
        }
    )
    rendered = "".join(segment.text for segment in parsed.segments)

    assert "hidden" not in rendered
    assert "┌" not in rendered and "┐" not in rendered
    assert rendered.strip() == "─" * 4


def test_positioned_html_rejects_unbounded_coordinates_and_area() -> None:
    document = {
        0: [
            html_division(500_000, 0, 100, 100, [html_text("coordinate")]),
            html_division(0, 0, 500_000, 500_000, [html_text("area")]),
            html_division(0, 0, -100, 100, [html_text("negative extent")]),
        ]
    }
    parsed = parse_line({0: 22, 1: False, 2: True, 3: True, 4: 0, 5: [variant(2, document)]})

    assert parsed.segments == ()


def test_plain_html_service_text_characterizes_visual_nodes_without_layout() -> None:
    document = {
        0: [
            html_division(
                2_000,
                1_000,
                500,
                500,
                [
                    html_text("A"),
                    html_shape("space", [html_length(100)]),
                    html_shape("rect", [html_length(100)]),
                    html_image(),
                ],
            )
        ]
    }
    raw = {0: 20, 1: False, 2: True, 3: True, 4: 0, 5: [variant(2, document)]}

    assert plain_line(raw) == "A  [图形]"


def test_erafl_color_line_rectangles_form_one_colored_rule() -> None:
    """Use COLOR_LINE's real 6200 + 100 + 100 PRINT_RECT sequence."""

    def parameters(width: int) -> list[object]:
        return [
            html_length(0),
            html_length(45),
            html_length(width),
            html_length(10),
        ]

    raw = {
        0: 13,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [
            variant(4, {0: "rect", 1: parameters(6_200), 2: color(30, 30, 30)}),
            variant(4, {0: "rect", 1: parameters(100), 2: color(30, 30, 30)}),
            variant(4, {0: "rect", 1: parameters(100), 2: color(17, 17, 17)}),
        ],
    }

    parsed = parse_line(raw)

    assert "".join(segment.text for segment in parsed.segments) == "━" * 128
    # Service text remains independent from the terminal-only visual projection.
    assert plain_line(raw) == "[图形]" * 3
    assert [(len(segment.text), segment.style.foreground) for segment in parsed.segments] == [
        (124, "#1e1e1e"),
        (2, "#1e1e1e"),
        (2, "#111111"),
    ]

    logical = parse_line(
        {
            0: 14,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(4, {0: "rect", 1: [variant(0, 16_000)], 2: color(1, 2, 3)})],
        }
    )
    assert "".join(segment.text for segment in logical.segments) == "━━"
    html_pixels = parse_line(
        {
            0: 15,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [
                variant(
                    2,
                    {0: [html_shape("rect", [html_length(16, pixels=True)])]},
                )
            ],
        }
    )
    assert "".join(segment.text for segment in html_pixels.segments) == "━━"


def test_direct_rectangle_validates_geometry_and_preserves_hover_color() -> None:
    valid = parse_line(
        {
            0: 15,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [
                variant(
                    4,
                    {
                        0: "rect",
                        1: [
                            html_length(50),
                            html_length(-50),
                            html_length(100),
                            html_length(10),
                        ],
                        2: color(1, 2, 3),
                        3: color(4, 5, 6),
                    },
                )
            ],
        }
    ).segments

    assert [segment.text for segment in valid] == [" ", "━━"]
    assert valid[-1].style.foreground == "#010203"
    assert valid[-1].hover_style is not None
    assert valid[-1].hover_style.foreground == "#040506"
    for parameters in (
        [html_length(-50), html_length(0), html_length(100), html_length(10)],
        [html_length(0), html_length(0), html_length(100), html_length(0)],
        [html_length(0), html_length(500_000), html_length(100), html_length(10)],
        [html_length(0), html_length(0), html_length(500_000), html_length(10)],
    ):
        parsed = parse_line(
            {
                0: 16,
                1: False,
                2: True,
                3: True,
                4: 0,
                5: [variant(4, {0: "rect", 1: parameters})],
            }
        )
        assert "".join(segment.text for segment in parsed.segments) == "[图形]"


def test_malformed_or_unsupported_shapes_keep_the_stable_fallback() -> None:
    parsed = parse_line(
        {
            0: 15,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(4, {0: "ellipse", 1: []}), variant(4, {0: "rect", 1: []})],
        }
    )

    assert "".join(segment.text for segment in parsed.segments) == "[图形][图形]"


def test_save_delete_button_becomes_a_red_right_edge_action() -> None:
    token = {0: 5, 1: 8}
    raw = {
        0: 11,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [
            variant(
                1,
                [
                    variant(
                        0,
                        "Delete save01.sav",
                        style(color(255, 255, 255)),
                        {0: 9, 1: [variant(1, "save01.sav")]},
                    )
                ],
                token,
                None,
                None,
                variant(1, ""),
                3,
                True,
            )
        ],
    }

    segment = parse_line(raw).segments[0]

    assert segment.text == "[X]"
    assert segment.token == token
    assert segment.title == "Delete save01.sav"
    assert segment.style.foreground == "#ef4444"
    assert segment.style.bold
    assert segment.right_edge


def test_width_independent_separator_uses_the_100_column_default() -> None:
    assert len(plain_line({5: [variant(6, "-")]})) == DEFAULT_VIEWPORT_COLUMNS == 100
    parsed = parse_line(
        {
            0: 1,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(6, "～", 0)],
        }
    )
    assert parsed.segments == ()
    assert parsed.layout == (SeparatorLayout(0, "～"),)
