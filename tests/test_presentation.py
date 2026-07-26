from rustyera_tui.presentation import (
    DEFAULT_VIEWPORT_COLUMNS,
    MAX_TABLE_COLUMN_WIDTH,
    MIN_TABLE_COLUMN_WIDTH,
    TARGET_TABLE_COLUMNS,
    VIEWPORT_BUFFER_LINES,
    ColumnCellLayout,
    PresentationModel,
    SeparatorLayout,
    ServicePresentationModel,
    coalesce_presentation_deltas,
    html_printed_str,
    parse_line,
    plain_line,
)
from rustyera_tui.wire import variant


def color(red: int, green: int, blue: int) -> dict[int, int]:
    return {0: red, 1: green, 2: blue, 3: 255}


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
        5: None,
        6: settings,
    }


def test_snapshot_preserves_color_and_button_token() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    assert model.title == "Game"
    assert model.lines[0].logical_line_start
    assert model.lines[0].segments[0].text == "开始"
    assert model.lines[0].segments[0].style.foreground == "#00ff80"
    assert model.lines[0].segments[0].token == {0: 1, 1: 1}


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
            variant(5, [button], 1, 8),
            variant(5, [variant(0, "中文", style(color(255, 255, 255)), None)], 0, 6),
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
                        variant(7, "ignored.png", None, None, None, None, None),
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


def test_button_generation_delta_disables_every_old_button_segment() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    segment = model.lines[0].segments[0]
    assert segment.generation == 0
    assert not segment.enabled


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

    assert [item.line_id for item in rich.lines] == [2, 3, 4]
    assert [item[0] for item in service.lines] == [2, 3, 4]
    assert rich.take_render_change() == (2, 1)


def test_main_viewport_keeps_only_the_newest_thousand_logical_lines() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[2][0] = [
        {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: []}
        for line_id in range(1, VIEWPORT_BUFFER_LINES + 3)
    ]
    initial[6][4] = 5_000

    model.apply_snapshot(initial)

    assert len(model.lines) == VIEWPORT_BUFFER_LINES
    assert [item.line_id for item in model.lines[:2]] == [3, 4]
    assert model.lines[-1].segments == ()
    assert model.maximum_physical_lines == VIEWPORT_BUFFER_LINES

    model.take_render_change()
    model.apply_delta({0: 1, 1: 2, 2: [variant(14, 2)]})
    assert len(model.lines) == VIEWPORT_BUFFER_LINES
    assert model.lines[0].line_id == 3
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
    assert [segment.enabled for item in combined.lines for segment in item.segments] == [
        False,
        False,
        True,
    ]
