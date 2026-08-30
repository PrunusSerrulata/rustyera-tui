import pytest
from rich.cells import cell_len

import rustyera_tui.presentation as presentation_module

from rustyera_tui.presentation import (
    DEFAULT_VIEWPORT_COLUMNS,
    MAX_TABLE_COLUMN_WIDTH,
    MIN_TABLE_COLUMN_WIDTH,
    TARGET_TABLE_COLUMNS,
    VIEWPORT_BUFFER_LINES,
    ColumnCellLayout,
    CellWidthIntent,
    PresentationDeltaAccumulator,
    PresentationEventAccumulator,
    PresentationModel,
    SeparatorLayout,
    ServicePresentationModel,
    coalesce_presentation_deltas,
    html_printed_str,
    parse_line,
    plain_line,
)
from rustyera_tui.wire import variant
from rustyera_tui.game_line_layout import project_responsive_segments

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


def color(red: int, green: int, blue: int) -> dict[int, int]:
    return {0: red, 1: green, 2: blue, 3: 255}


def terminal_column(row: str, text: str) -> int:
    return cell_len(row[: row.index(text)])


def style(foreground: dict[int, int]) -> dict[int, object]:
    return {0: foreground, 2: False, 3: False, 4: False, 5: False, 7: 12_000}


def line(line_id: int, text: str, generation: int = 0) -> dict[int, object]:
    button = variant(
        1,
        [variant(0, text, style(color(0, 255, 128)), None)],
        {0: 1, 1: line_id},
        "选择",
        None,
        variant(0, line_id),
        generation,
        True,
    )
    return {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: [button]}


def snapshot() -> dict[int, object]:
    settings = {
        0: 100_000,
        1: 1_000,
        2: color(0, 0, 0),
        3: color(255, 255, 255),
        4: 1000,
        5: True,
        6: False,
    }
    return {
        0: 1,
        1: "Game",
        2: {0: [line(1, "开始")], 1: []},
        3: {0: 0, 1: []},
        5: None,
        6: settings,
    }


def test_whole_line_background_fields_are_preserved_from_protocol() -> None:
    raw = line(1, "eligible")
    raw[6] = True
    parsed = parse_line(raw)
    assert parsed.text_background_eligible is True

    value = snapshot()
    value[6][8] = {0: 17, 1: 34, 2: 51, 3: 127}
    model = PresentationModel()
    model.apply_snapshot(value)
    assert model.text_line_background == ("#112233", 127)


def test_snapshot_preserves_color_and_button_token() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    assert model.title == "Game"
    assert model.lines[0].logical_line_start
    assert model.lines[0].segments[0].text == "开始"
    assert model.lines[0].segments[0].style.foreground == "#00ff80"
    assert model.lines[0].segments[0].token == {0: 1, 1: 1}


def test_runtime_text_layout_keeps_service_text_raw_and_columns_recursive() -> None:
    token = {0: 3, 1: 4}
    button = variant(
        1,
        [variant(8, "■", style(color(255, 255, 255)), None, 2)],
        token,
        None,
        None,
        variant(0, 1),
        0,
        True,
    )
    raw = {
        0: 1,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [
            variant(5, [button], 0, variant(0, 2)),
            variant(8, "……", style(color(255, 255, 255)), None, 4),
        ],
    }

    parsed = parse_line(raw)

    assert [(segment.text, segment.logical_columns) for segment in parsed.segments] == [
        ("■", 2),
        ("……", 4),
    ]
    assert parsed.segments[0].token == token
    assert plain_line(raw) == "■……"
    assert html_printed_str([raw], 0) == "<p align='left'><nobr>■……</nobr></p>"


def test_tui_table_width_policy_uses_the_game_menu_bounds() -> None:
    assert MIN_TABLE_COLUMN_WIDTH == 16
    assert MAX_TABLE_COLUMN_WIDTH == 24
    assert TARGET_TABLE_COLUMNS == 5


def test_column_cells_preserve_semantic_layout_until_rendering() -> None:
    token = {0: 7, 1: 9}
    button = variant(
        1,
        [variant(0, "界[1]", style(color(0, 255, 128)), None)],
        token,
        "选择",
        None,
        variant(0, 1),
        0,
        True,
    )
    raw = {
        0: 4,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [
            variant(5, [button], 1, variant(0, 8)),
            variant(
                5,
                [variant(0, "中文", style(color(255, 255, 255)), None)],
                0,
                variant(0, 6),
            ),
        ],
    }

    parsed = parse_line(raw)

    assert "".join(segment.text for segment in parsed.segments) == "界[1]中文"
    assert parsed.segments[0].text == "界[1]"
    assert parsed.segments[0].token == token
    assert parsed.segments[0].title == "选择"
    assert parsed.layout == (
        ColumnCellLayout(0, 1, 1, 8),
        ColumnCellLayout(1, 2, 0, 6),
    )
    assert plain_line(raw) == "   界[1]中文  "


def test_logical_pixel_cells_use_a_fixed_terminal_approximation() -> None:
    raw = {
        0: 5,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [
            variant(5, [variant(0, "A", style(color(255, 255, 255)), None)], 1, variant(1, 80)),
            variant(5, [variant(0, "B", style(color(255, 255, 255)), None)], 0, variant(1, 40)),
        ],
    }

    parsed = parse_line(raw)

    assert parsed.layout == (
        ColumnCellLayout(0, 1, 1, 80, CellWidthIntent.LOGICAL_PIXELS),
        ColumnCellLayout(1, 2, 0, 40, CellWidthIntent.LOGICAL_PIXELS),
    )
    assert plain_line(raw) == "         AB    "
    assert "".join(segment.text for segment in project_responsive_segments(parsed, 15)) == (
        "         AB    "
    )
    assert "".join(segment.text for segment in project_responsive_segments(parsed, 14)) == (
        "         A\nB    "
    )


def test_column_width_wire_values_are_u32_and_terminal_padding_is_bounded() -> None:
    oversized = line(5, "")
    oversized[5] = [variant(5, [variant(0, "A")], 0, variant(1, 0x1_0000_0000))]
    with pytest.raises(ValueError, match="fit u32"):
        parse_line(oversized)

    maximum = line(6, "")
    maximum[5] = [variant(5, [variant(0, "A")], 0, variant(0, 0xFFFF_FFFF))]
    projected = plain_line(maximum)
    assert projected.startswith("A")
    assert len(projected) == 4_096


def test_auto_height_snake_division_preserves_terminal_text_and_buttons() -> None:
    token = {0: 14, 1: 15}
    document = {
        0: [
            variant(
                1,
                12,
                [],
                [html_button([html_text("继续")], token, "继续游戏")],
                None,
                0,
                0,
                variant(9, None, None, html_length(80, pixels=True), None, 3, None, 0, {}),
            )
        ]
    }
    raw = {
        0: 6,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [variant(2, document)],
    }

    parsed = parse_line(raw)

    assert "".join(segment.text for segment in parsed.segments) == "继续"
    assert [segment.token for segment in parsed.segments] == [token]
    assert parsed.segments[0].title == "继续游戏"


def test_absolute_division_ignores_unreliable_coordinates_but_keeps_source_order() -> None:
    token = {0: 18, 1: 19}
    document = {
        0: [
            variant(
                1,
                12,
                [],
                [html_text("前"), html_button([html_text("继续")], token, "继续游戏"), html_text("后")],
                None,
                0,
                0,
                variant(
                    9,
                    html_length(9_999_999, pixels=True),
                    html_length(-9_999_999, pixels=True),
                    html_length(80, pixels=True),
                    html_length(32, pixels=True),
                    3,
                    None,
                    3,
                    {},
                ),
            )
        ]
    }
    raw = line(7, "")
    raw[5] = [variant(2, document)]

    parsed = parse_line(raw)

    assert "".join(segment.text for segment in parsed.segments) == "前继续后"
    assert [segment.token for segment in parsed.segments] == [None, token, None]


def test_positioned_division_rejects_legacy_bool_mode_and_invalid_height() -> None:
    legacy = line(8, "")
    legacy[5] = [
        variant(
            2,
            {
                0: [
                    variant(
                        1,
                        12,
                        [],
                        [html_text("bad")],
                        None,
                        0,
                        0,
                        variant(9, None, None, html_length(80, pixels=True), None, 0, None, True, {}),
                    )
                ]
            },
        )
    ]
    with pytest.raises(ValueError, match="display mode"):
        parse_line(legacy)

    invalid_height = line(9, "")
    invalid_height[5] = [
        variant(
            2,
            {
                0: [
                    variant(
                        1,
                        12,
                        [],
                        [html_text("bad")],
                        None,
                        0,
                        0,
                        variant(9, None, None, html_length(80, pixels=True), html_length(0, pixels=True), 0, None, 0, {}),
                    )
                ]
            },
        )
    ]
    with pytest.raises(ValueError, match="height must be positive"):
        parse_line(invalid_height)


def scene_layer(
    layer_id: int,
    sequence: int,
    depth: int,
    *,
    revision: int,
    line_id: int | None = None,
    interaction: dict[int, object] | None = None,
) -> dict[int, object]:
    return {
        0: layer_id,
        1: sequence,
        2: variant(1, f"sprite-{layer_id}", 1),
        3: depth,
        4: variant(0) if line_id is None else variant(1, line_id),
        5: {0: 0, 1: 0},
        6: {0: 1_000, 1: 1_000},
        7: 255,
        9: 0,
        10: interaction,
        11: revision,
        12: 0,
    }


def test_scene_snapshot_and_deltas_replay_without_exposing_pixel_interactions() -> None:
    value = snapshot()
    value[3] = {
        0: 1,
        1: [
            scene_layer(2, 2, 3, revision=1, line_id=9),
            scene_layer(
                1,
                1,
                8,
                revision=1,
                interaction={
                    0: {0: 7, 1: 8},
                    1: variant(0, 42),
                    2: True,
                    5: "pixel-only",
                },
            ),
        ],
    }
    model = PresentationModel()
    model.apply_snapshot(value)

    assert [layer[0] for layer in model.scene[1]] == [1, 2]
    assert [segment.text for line_model in model.lines for segment in line_model.segments] == [
        "开始"
    ]
    assert not model.has_enabled_button({0: 7, 1: 8})

    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [
                variant(
                    4,
                    {
                        0: 1,
                        1: 2,
                        2: [
                            variant(0, scene_layer(1, 1, 9, revision=2)),
                            variant(3, 9),
                        ],
                    },
                )
            ],
        }
    )

    assert model.scene[0] == 2
    assert [(layer[0], layer[3]) for layer in model.scene[1]] == [(1, 9)]
    assert model.lines[0].segments[0].text == "开始"

    resync = snapshot()
    resync[0] = 2
    resync[3] = model.scene
    reconnected = PresentationModel()
    reconnected.apply_snapshot(resync)
    assert reconnected.scene == model.scene
    assert reconnected.lines == model.lines


def test_invalid_scene_delta_is_atomic_and_accumulator_preserves_revision_chain() -> None:
    first = {
        0: 1,
        1: 2,
        2: [variant(4, {0: 0, 1: 1, 2: [variant(0, scene_layer(1, 1, 2, revision=1))]})],
    }
    second = {0: 2, 1: 3, 2: [variant(4, {0: 1, 1: 2, 2: [variant(1, 99)]})]}
    accumulator = PresentationDeltaAccumulator()
    accumulator.add(first)
    accumulator.add(second)
    combined = accumulator.take()
    assert combined is not None
    assert [operation[0] for operation in combined[2]] == [4, 4]

    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta(combined)
    assert [(layer[0], layer[1]) for layer in model.scene[1]] == [(1, 1)]

    before = model.scene
    with pytest.raises(ValueError, match="changed its insertion sequence"):
        model.apply_delta(
            {
                0: 3,
                1: 4,
                2: [
                    variant(
                        4,
                        {
                            0: 2,
                            1: 3,
                            2: [
                                variant(0, scene_layer(2, 2, 1, revision=3)),
                                variant(0, scene_layer(1, 99, 1, revision=3)),
                            ],
                        },
                    )
                ],
            }
        )
    assert model.scene is before
    assert model.revision == 3


def test_service_scene_validation_precedes_delivery_and_rejects_malformed_scalars() -> None:
    service = ServicePresentationModel()
    initial = snapshot()
    initial[3] = {0: 1, 1: [scene_layer(1, 1, 1, revision=1)]}
    service.apply_snapshot(initial)
    assert service.scene[0] == 1

    malformed = scene_layer(
        2,
        2,
        1,
        revision=2,
        interaction={
            0: {0: 1, 1: 2},
            1: variant(0, 1),
            2: 1,
        },
    )
    before = service.scene
    with pytest.raises(TypeError, match="enabled must be a boolean"):
        service.apply_delta(
            {0: 1, 1: 2, 2: [variant(4, {0: 1, 1: 2, 2: [variant(0, malformed)]})]}
        )
    assert service.scene is before
    assert service.revision == 1

    missing_scene = snapshot()
    del missing_scene[3]
    with pytest.raises(ValueError, match="missing scene state"):
        service.apply_snapshot(missing_scene)

    invalid_scroll = snapshot()
    invalid_scroll[3] = {0: 1, 1: [scene_layer(1, 1, 1, revision=1)]}
    invalid_scroll[3][1][0][9] = True
    with pytest.raises(ValueError, match="scroll policy"):
        service.apply_snapshot(invalid_scroll)

    missing_origin = snapshot()
    layer_without_origin = scene_layer(1, 1, 1, revision=1)
    del layer_without_origin[12]
    missing_origin[3] = {0: 1, 1: [layer_without_origin]}
    with pytest.raises(TypeError, match="document origin"):
        service.apply_snapshot(missing_origin)


def test_html_printed_str_groups_wrapped_rows_from_the_newest_line() -> None:
    lines = [
        {0: 1, 2: True, 4: 0, 5: [variant(0, "old")]},
        {0: 2, 2: True, 4: 1, 5: [variant(0, "A&B")]},
        {0: 3, 2: False, 4: 1, 5: [variant(0, "<tail>")]},
    ]

    assert html_printed_str(lines, 0) == (
        "<p align='center'><nobr>A&amp;B<br>&lt;tail&gt;</nobr></p>"
    )
    assert html_printed_str(lines, 1) == "<p align='left'><nobr>old</nobr></p>"
    assert html_printed_str(lines, 2) == ""


def test_structured_html_preserves_rows_styles_spaces_and_buttons() -> None:
    token = {0: 8, 1: 13}
    html = {
        0: [
            variant(
                1,
                5,
                [{0: "align", 1: "left"}],
                [
                    variant(1, 13, [], [], None, 0, 0, variant(10)),
                    variant(
                        1,
                        11,
                        [],
                        [],
                        None,
                        0,
                        0,
                        variant(8, "space", [variant(1, 100)], None, None),
                    ),
                    variant(
                        1,
                        10,
                        [{0: "src", 1: "ignored.png"}],
                        [],
                        None,
                        0,
                        0,
                        variant(
                            7,
                            "ignored.png",
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            0,
                            None,
                        ),
                    ),
                    variant(
                        1,
                        7,
                        [{0: "value", 1: "0"}],
                        [
                            variant(0, "[  0] ", 0, 0),
                            variant(
                                1,
                                0,
                                [],
                                [variant(0, "选择", 0, 0)],
                                None,
                                0,
                                0,
                                variant(0),
                            ),
                        ],
                        {0: 8, 1: 13, 2: 0, 4: 2, 5: True},
                        0,
                        0,
                        variant(4, "0", "选项", None),
                    ),
                    variant(1, 13, [], [], None, 0, 0, variant(10)),
                ],
                None,
                0,
                0,
                variant(2, 0),
            )
        ]
    }
    raw = {
        0: 9,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [variant(2, html)],
    }

    parsed = parse_line(raw)

    assert "".join(segment.text for segment in parsed.segments) == "\n  [  0] 选择\n"
    button_segments = [segment for segment in parsed.segments if segment.token == token]
    assert "".join(segment.text for segment in button_segments) == "[  0] 选择"
    assert all(segment.generation == 2 and segment.title == "选项" for segment in button_segments)
    assert any(segment.style.bold for segment in button_segments)
    assert plain_line(raw) == "\n  [  0] 选择\n"


def test_tagged_html_preserves_wide_box_drawing_cells_for_terminal_tables() -> None:
    raw = {
        0: 10,
        1: False,
        2: True,
        3: True,
        4: 0,
        5: [
            variant(
                2,
                {
                    0: [
                        html_text("┌──"),
                        html_button([html_text("角色")], {0: 8, 1: 14}, "选择角色"),
                        html_text("┐"),
                    ]
                },
            )
        ],
    }

    parsed = parse_line(raw)

    assert "".join(segment.text for segment in parsed.segments) == "┌──角色┐"
    assert "".join(segment.text for segment in parsed.segments if segment.token) == "角色"
    assert (
        sum(segment.logical_columns or cell_len(segment.text) for segment in parsed.segments) == 12
    )


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


def test_delta_append_replace_delete_and_revision_check() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "继续"))]})
    assert [item.line_id for item in model.lines] == [1, 2]
    model.apply_delta({0: 2, 1: 3, 2: [variant(7, 2, line(2, "载入"))]})
    assert model.lines[-1].segments[0].text == "载入"
    model.apply_delta({0: 3, 1: 4, 2: [variant(1, 1)]})
    assert [item.line_id for item in model.lines] == [1]


def test_delta_can_clear_an_optional_input_wait() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[5] = {0: 9, 1: 2, 11: {0: 1, 1: 7}}
    model.apply_snapshot(initial)

    # minicbor omits a `None` tuple field, so SetInputWait(None) has no fields.
    model.apply_delta({0: 1, 1: 2, 2: [variant(6)]})

    assert model.input_wait is None


def test_button_generation_delta_lazily_disables_every_old_button_segment() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    segment = model.lines[0].segments[0]
    assert segment.generation == 0
    assert segment.enabled
    assert not model.segment_enabled(segment)


def test_partial_line_updates_keep_current_buttons_and_retire_late_old_buttons() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [
                variant(13, 1),
                variant(7, 1, line(1, "current map", generation=1)),
                variant(0, line(2, "late history", generation=0)),
            ],
        }
    )

    current, stale = (item.segments[0] for item in model.lines)
    assert model.segment_enabled(current)
    assert current.generation == 1
    assert not model.segment_enabled(stale)
    assert stale.generation == 0


def test_snapshot_forgets_local_button_generation_before_later_partial_updates() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    resynchronized = snapshot()
    resynchronized[0] = 3
    resynchronized[2] = {0: [line(1, "snapshot", generation=2)], 1: []}
    model.apply_snapshot(resynchronized)
    model.apply_delta(
        {
            0: 3,
            1: 4,
            2: [
                variant(7, 1, line(1, "replacement", generation=2)),
                variant(0, line(2, "append", generation=2)),
            ],
        }
    )

    assert all(segment.enabled for item in model.lines for segment in item.segments)


def test_submitted_buttons_retire_while_later_partial_updates_stay_enabled() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    retired = model.retire_presented_interactions()
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "dynamic map"))]})

    assert not model.segment_enabled(model.lines[0].segments[0])
    assert model.segment_enabled(model.lines[1].segments[0])

    resynchronized = snapshot()
    resynchronized[0] = 3
    model.apply_snapshot(resynchronized)
    assert not model.segment_enabled(model.lines[0].segments[0])

    model.restore_interaction_boundary(retired)
    assert model.segment_enabled(model.lines[0].segments[0])


def test_unchanged_submitted_buttons_rearm_without_reviving_replacements() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    unchanged_boundary = model.retire_presented_interactions()
    assert model.restore_submitted_interaction_boundary(unchanged_boundary)
    assert model.segment_enabled(model.lines[0].segments[0])

    replaced_boundary = model.retire_presented_interactions()
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "dynamic map"))]})
    assert not model.restore_submitted_interaction_boundary(replaced_boundary)
    assert not model.segment_enabled(model.lines[0].segments[0])
    assert model.segment_enabled(model.lines[1].segments[0])

    current_boundary = model.retire_presented_interactions()
    model.apply_delta({0: 2, 1: 3, 2: [variant(13, 1)]})
    assert model.restore_submitted_interaction_boundary(current_boundary)
    assert not model.segment_enabled(model.lines[1].segments[0])


def test_button_policy_changes_do_not_rewrite_accumulated_history() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[2] = {0: [line(index, f"line {index}") for index in range(1, 5_001)], 1: []}
    initial[6][4] = 5_000
    model.apply_snapshot(initial)
    before = tuple(id(item) for item in model.lines)
    model.take_render_change()

    retired = model.retire_presented_interactions()
    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    assert tuple(id(item) for item in model.lines) == before
    assert model.take_render_change() == (len(model.lines), 0)
    assert not model.segment_enabled(model.lines[-1].segments[0])

    model.apply_delta({0: 2, 1: 3, 2: [variant(0, line(5_001, "new", generation=1))]})
    assert model.segment_enabled(model.lines[-1].segments[0])
    model.restore_interaction_boundary(retired)
    assert not model.segment_enabled(model.lines[0].segments[0])


def test_line_index_stays_valid_across_replace_delete_and_append() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [variant(0, line(2, "two")), variant(0, line(3, "three"))],
        }
    )
    model.apply_delta(
        {
            0: 2,
            1: 3,
            2: [variant(1, 2), variant(0, line(4, "four")), variant(7, 1, line(1, "one"))],
        }
    )

    assert [item.line_id for item in model.lines] == [1, 4]
    assert model.lines[0].segments[0].text == "one"
    assert model._line_indices == {1: 0, 4: 1}


def test_trim_lines_removes_the_oldest_history_and_reports_incremental_hints() -> None:
    rich = PresentationModel()
    service = ServicePresentationModel()
    initial = snapshot()
    initial[2][0] = [line(1, "one"), line(2, "two"), line(3, "three")]
    rich.apply_snapshot(initial)
    service.apply_snapshot(initial)
    rich.take_render_change()
    delta = {
        0: 1,
        1: 2,
        2: [variant(14, 1), variant(0, line(4, "four"))],
    }

    rich.apply_delta(delta)
    service.apply_delta(delta)
    replacement = {
        0: 2,
        1: 3,
        2: [variant(7, 2, line(2, "TWO"))],
    }
    rich.apply_delta(replacement)
    service.apply_delta(replacement)

    assert [item.line_id for item in rich.lines] == [2, 3, 4]
    assert [item.line_id for item in service.lines] == [2, 3, 4]
    assert rich.lines[0].segments[0].text == "TWO"
    assert plain_line(service.lines[0]) == "TWO"
    # The replacement targets the first retained row after the prefix trim. The trim hint lets
    # the viewport discard the old prefix, while changed_from remains relative to the new list.
    assert rich.take_render_change() == (0, 1)


def test_main_viewport_tracks_the_runtime_max_log_setting() -> None:
    model = PresentationModel()
    initial = snapshot()
    configured_limit = 1_500
    initial[2][0] = [
        {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: []}
        for line_id in range(1, configured_limit + 3)
    ]
    initial[6][4] = configured_limit

    model.apply_snapshot(initial)

    assert len(model.lines) == configured_limit
    assert [item.line_id for item in model.lines[:2]] == [3, 4]
    assert model.lines[-1].segments == ()
    assert model.maximum_physical_lines == configured_limit

    model.take_render_change()
    model.apply_delta({0: 1, 1: 2, 2: [variant(14, 2)]})
    assert len(model.lines) == configured_limit
    assert model.lines[0].line_id == 3
    assert model.take_render_change() == (None, 0)


def test_snapshot_parses_only_the_retained_viewport_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = PresentationModel()
    initial = snapshot()
    configured_limit = 500
    initial[2][0] = [line(line_id, str(line_id)) for line_id in range(1, 701)]
    initial[6][4] = configured_limit
    parsed: list[int] = []
    original_parse_line = presentation_module.parse_line

    def tracked_parse_line(raw: dict[int, object]):
        parsed.append(int(raw[0]))
        return original_parse_line(raw)

    monkeypatch.setattr(presentation_module, "parse_line", tracked_parse_line)

    model.apply_snapshot(initial)

    assert parsed == list(range(201, 701))
    assert [item.line_id for item in model.lines] == parsed
    model.take_render_change()
    model.apply_delta({0: 1, 1: 2, 2: [variant(14, 200)]})
    assert [item.line_id for item in model.lines] == parsed
    assert model.take_render_change() == (None, 0)


def test_viewport_limit_trims_incremental_blank_lines_with_render_hints() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[2][0] = [
        {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: []}
        for line_id in range(1, VIEWPORT_BUFFER_LINES + 1)
    ]
    model.apply_snapshot(initial)
    model.take_render_change()

    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [
                variant(
                    0,
                    {
                        0: VIEWPORT_BUFFER_LINES + 1,
                        1: False,
                        2: True,
                        3: True,
                        4: 0,
                        5: [],
                    },
                )
            ],
        }
    )

    assert len(model.lines) == VIEWPORT_BUFFER_LINES
    assert model.lines[0].line_id == 2
    assert model.lines[-1].segments == ()
    assert model.take_render_change() == (VIEWPORT_BUFFER_LINES - 1, 1)


def test_raw_worker_projection_tracks_rich_text_and_wait_state() -> None:
    rich = PresentationModel()
    service = ServicePresentationModel()
    initial = snapshot()
    rich.apply_snapshot(initial)
    service.apply_snapshot(initial)
    delta = {
        0: 1,
        1: 2,
        2: [variant(7, 1, line(1, "loaded")), variant(6, {0: 2, 1: 2})],
    }
    rich.apply_delta(delta)
    service.apply_delta(delta)

    assert [plain_line(line) for line in service.lines] == [
        "".join(segment.text for segment in rich.lines[0].segments)
    ]
    assert service.input_wait == rich.input_wait == {0: 2, 1: 2}


def test_service_projection_compacts_lines_and_suppresses_retired_history_until_clear() -> None:
    service = ServicePresentationModel()
    initial = snapshot()
    service.apply_snapshot(initial)

    assert service.lines[0].text == "开始"
    assert not isinstance(service.lines[0], dict)
    with pytest.raises(ValueError, match="service limit"):
        service.html_printed_str(0, 8)

    retired_revision = service.retire_history()
    stale = service.apply_delta(
        {0: retired_revision, 1: 2, 2: [variant(0, line(2, "stale"))]}
    )
    assert stale[2] == []
    assert service.lines == []

    replacement = service.apply_delta(
        {
            0: 2,
            1: 3,
            2: [variant(2), variant(0, line(3, "title"))],
        }
    )
    assert replacement[2]
    assert [item.text for item in service.lines] == ["title"]


def test_viewport_snapshot_enforces_line_byte_and_segment_hard_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presentation_module, "MAXIMUM_VIEWPORT_UTF8_BYTES", 8)
    monkeypatch.setattr(presentation_module, "MAXIMUM_VIEWPORT_SEGMENTS", 2)
    model = PresentationModel()
    initial = snapshot()
    initial[6][4] = 1_000_000
    initial[2][0] = [
        {
            0: line_id,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(0, text, None, None)],
        }
        for line_id, text in ((1, "aaaaa"), (2, "bbbbb"), (3, "cc"))
    ]

    model.apply_snapshot(initial)

    assert model.maximum_physical_lines == presentation_module.MAXIMUM_VIEWPORT_BUFFER_LINES
    assert [item.line_id for item in model.lines] == [2, 3]
    assert model._retained_utf8_bytes <= presentation_module.MAXIMUM_VIEWPORT_UTF8_BYTES
    assert model._retained_segments <= presentation_module.MAXIMUM_VIEWPORT_SEGMENTS


def test_display_line_cost_includes_button_titles_and_separator_patterns() -> None:
    button_line = parse_line(line(1, "x"))
    separator_line = parse_line(
        {0: 2, 1: False, 2: True, 3: True, 4: 0, 5: [variant(6, "～", 0)]}
    )

    assert button_line._retained_utf8_bytes == len("x选择".encode())
    assert button_line._retained_segments == 1
    assert separator_line._retained_utf8_bytes == len("～".encode())
    assert separator_line._retained_segments == 1


def test_rejected_replacement_stays_retired_until_resync_snapshot() -> None:
    service = ServicePresentationModel()
    service.apply_snapshot(snapshot())
    revision = service.begin_replacement(41)

    assert service.reject_replacement(41)
    filtered = service.apply_delta(
        {0: revision, 1: revision + 1, 2: [variant(0, line(2, "stale"))]}
    )
    assert filtered[2] == []
    assert service.lines == []

    replacement = snapshot()
    replacement[0] = revision + 1
    replacement[2] = {0: [line(3, "resynchronized")], 1: []}
    service.apply_snapshot(replacement)
    service.apply_delta(
        {0: revision + 1, 1: revision + 2, 2: [variant(0, line(4, "new"))]}
    )
    assert [item.text for item in service.lines] == ["resynchronized", "new"]


def test_rich_replacement_boundary_clears_on_clear_history() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.retire_history(model.revision)

    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "stale"))]})
    assert model.lines == []
    model.apply_delta(
        {
            0: 2,
            1: 3,
            2: [variant(2), variant(0, line(3, "replacement"))],
        }
    )

    assert [item.line_id for item in model.lines] == [3]


def test_delta_coalescing_preserves_state_and_discards_superseded_lines() -> None:
    original = [
        {
            0: 1,
            1: 2,
            2: [variant(0, line(2, "draft")), variant(7, 1, line(1, "old"))],
        },
        {
            0: 2,
            1: 3,
            2: [
                variant(7, 2, line(2, "final")),
                variant(7, 1, line(1, "new")),
                variant(6, {0: 4}),
                variant(6),
            ],
        },
    ]
    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in original:
        sequential.apply_delta(delta)
    combined = PresentationModel()
    combined.apply_snapshot(snapshot())
    coalesced = coalesce_presentation_deltas(original)
    combined.apply_delta(coalesced)

    assert combined == sequential
    assert len(coalesced[2]) < sum(len(delta[2]) for delta in original)


def test_delta_coalescing_preserves_button_generation_order() -> None:
    original = [
        {
            0: 1,
            1: 2,
            2: [variant(13, 1), variant(0, line(2, "one", generation=1))],
        },
        {
            0: 2,
            1: 3,
            2: [variant(13, 2), variant(0, line(3, "two", generation=2))],
        },
    ]
    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in original:
        sequential.apply_delta(delta)
    combined = PresentationModel()
    combined.apply_snapshot(snapshot())
    combined.apply_delta(coalesce_presentation_deltas(original))

    assert combined == sequential
    assert [
        combined.segment_enabled(segment) for item in combined.lines for segment in item.segments
    ] == [
        False,
        False,
        True,
    ]


def test_incremental_delta_accumulator_crosses_two_former_compaction_windows_once() -> None:
    deltas = [
        {
            0: revision,
            1: revision + 1,
            2: [
                variant(0, line(2, "value 0"))
                if revision == 1
                else variant(7, 2, line(2, f"value {revision}"))
            ],
        }
        for revision in range(1, 131)
    ]
    accumulator = PresentationDeltaAccumulator()
    for delta in deltas:
        accumulator.add(delta)

    combined_delta = accumulator.take()
    assert combined_delta is not None
    assert len(combined_delta[2]) == 1
    assert accumulator.take() is None

    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in deltas:
        sequential.apply_delta(delta)
    accumulated = PresentationModel()
    accumulated.apply_snapshot(snapshot())
    accumulated.apply_delta(combined_delta)
    assert accumulated == sequential


def test_presentation_event_accumulator_snapshot_discards_older_delta_state() -> None:
    accumulator = PresentationEventAccumulator()
    accumulator.add_delta({0: 1, 1: 2, 2: [variant(0, line(2, "discarded"))]})
    replacement = snapshot()
    replacement[0] = 20
    accumulator.replace_snapshot(replacement)
    following = {0: 20, 1: 21, 2: [variant(0, line(3, "retained"))]}
    accumulator.add_delta(following)

    assert accumulator.take() == (replacement, following)
    assert accumulator.take() == (None, None)


def test_incremental_accumulator_matches_destructive_and_stateful_delta_sequence() -> None:
    updated_settings = dict(snapshot()[6])
    updated_settings[0] = 500_000
    original = [
        {
            0: 1,
            1: 2,
            2: [
                variant(0, line(2, "two")),
                variant(3, "first title"),
                variant(8, updated_settings),
            ],
        },
        {
            0: 2,
            1: 3,
            2: [
                variant(7, 2, line(2, "replacement")),
                variant(1, 1),
                variant(0, line(3, "three", generation=1)),
                variant(13, 1),
                variant(6, {0: 5, 1: 2}),
            ],
        },
        {
            0: 3,
            1: 4,
            2: [
                variant(2),
                variant(0, line(4, "after clear", generation=1)),
                variant(14, 1),
                variant(3, "final title"),
                variant(6),
            ],
        },
    ]
    accumulator = PresentationDeltaAccumulator()
    for delta in original:
        accumulator.add(delta)
    combined_delta = accumulator.take()
    assert combined_delta is not None

    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in original:
        sequential.apply_delta(delta)
    accumulated = PresentationModel()
    accumulated.apply_snapshot(snapshot())
    accumulated.apply_delta(combined_delta)

    assert accumulated == sequential
