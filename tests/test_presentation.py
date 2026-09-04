import pytest
from rich.cells import cell_len

from erafl_layout_fixture import html_button, html_length, html_text
from presentation_test_support import color, line, snapshot, style
from rustyera_tui.game_line_layout import project_responsive_segments
from rustyera_tui.presentation import (
    MAX_TABLE_COLUMN_WIDTH,
    MIN_TABLE_COLUMN_WIDTH,
    TARGET_TABLE_COLUMNS,
    CellWidthIntent,
    ColumnCellLayout,
    PresentationDeltaAccumulator,
    PresentationModel,
    ServicePresentationModel,
    html_printed_str,
    parse_line,
    plain_line,
)
from rustyera_tui.wire import variant


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
                [
                    html_text("前"),
                    html_button([html_text("继续")], token, "继续游戏"),
                    html_text("后"),
                ],
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
                        variant(
                            9, None, None, html_length(80, pixels=True), None, 0, None, True, {}
                        ),
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
                        variant(
                            9,
                            None,
                            None,
                            html_length(80, pixels=True),
                            html_length(0, pixels=True),
                            0,
                            None,
                            0,
                            {},
                        ),
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
        service.apply_delta({0: 1, 1: 2, 2: [variant(4, {0: 1, 1: 2, 2: [variant(0, malformed)]})]})
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
