from __future__ import annotations

from dataclasses import replace

from pytest import MonkeyPatch

from app_test_support import (
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    FakeWorker,
    GUILD_TASK_TOKEN,
    GameLine,
    GameViewport,
    Path,
    RustyEraTui,
    SeparatorLayout,
    cell_len,
    erafl_guild_line,
    parse_line,
    variant,
)
from rustyera_tui import widgets
from rustyera_tui.game_line_layout import project_html_box_rows, terminal_segment_text


async def test_horizontal_scrollbar_replaces_the_prompt_separator(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    short = DisplayLineModel(1, False, True, True, 0, (DisplaySegment("short"),))
    long = DisplayLineModel(1, False, True, True, 0, (DisplaySegment("x" * 240),))
    async with app.run_test(size=(160, 30)) as pilot:
        viewport = app.query_one(GameViewport)
        separator = app.query_one("#separator-line")

        await viewport.set_lines([short])
        await pilot.pause()
        assert not viewport.show_horizontal_scrollbar
        assert separator.display

        await viewport.set_lines([long])
        await pilot.pause()
        assert viewport.show_horizontal_scrollbar
        assert not separator.display

        await viewport.set_lines([short])
        await pilot.pause()
        assert not viewport.show_horizontal_scrollbar
        assert separator.display


async def test_column_cells_reflow_around_the_five_column_target(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    segments = tuple(DisplaySegment(f"[{index}]") for index in range(8))
    line = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        segments,
        tuple(ColumnCellLayout(index, index + 1, 0, 25) for index in range(8)),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([line])
        await pilot.pause()
        game_line = app.query_one(GameLine)
        identity = id(game_line)

        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [95, 57]

        await pilot.resize_terminal(80, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [76, 76]

        await pilot.resize_terminal(79, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [76, 76]

        await pilot.resize_terminal(59, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [57, 57, 38]
        assert id(app.query_one(GameLine)) == identity

        await pilot.resize_terminal(24, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [22] * 8

        await pilot.resize_terminal(15, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [16] * 8
        assert viewport.show_horizontal_scrollbar

        await pilot.resize_terminal(120, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [115, 69]

        await pilot.resize_terminal(121, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [115, 69]

        await pilot.resize_terminal(143, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [120, 72]

        await pilot.resize_terminal(144, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [120, 72]

        await pilot.resize_terminal(146, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [144, 48]


async def test_responsive_layout_preserves_long_text_maps_and_button_coordinates(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    tokens = tuple({0: 8, 1: index} for index in range(5))
    table = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        tuple(
            DisplaySegment(f"[{index}] option", token=token) for index, token in enumerate(tokens)
        ),
        tuple(ColumnCellLayout(index, index + 1, 0, 25) for index in range(5)),
    )
    long_cell = DisplayLineModel(
        2,
        False,
        True,
        True,
        0,
        (DisplaySegment("x" * 24),),
        (ColumnCellLayout(0, 1, 0, 25),),
    )
    map_line = DisplayLineModel(
        3,
        False,
        True,
        True,
        0,
        (DisplaySegment("┌" + "─" * 39 + "┐"),),
    )
    app.presentation.lines = [table, long_cell, map_line]
    app.active_wait = {0: 8, 1: 2}
    async with app.run_test(size=(59, 30)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(app.presentation.lines)
        await pilot.pause()
        game_lines = list(app.query(GameLine))

        assert [region.row for region in game_lines[0].regions] == [0, 0, 0, 1, 1]
        assert game_lines[1].render().plain == "x" * 24
        assert "\n" not in game_lines[2].render().plain
        assert game_lines[2].render().plain == "┌" + "─" * 39 + "┐"
        assert await pilot.click(game_lines[0], offset=(20, 1))
        assert ("activate", tokens[4]) in worker.commands
        map_content = game_lines[2].content

        await pilot.resize_terminal(40, 30)
        assert game_lines[2].content is map_content
        assert game_lines[2].render().plain == "┌" + "─" * 39 + "┐"
        assert viewport.show_horizontal_scrollbar


async def test_positioned_html_table_keeps_clickable_terminal_coordinates(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    model = parse_line(erafl_guild_line())
    app.presentation.lines = [model]
    app.active_wait = {0: 8, 1: 2}

    async with app.run_test(size=(180, 60)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([model])
        await pilot.pause()
        game_line = app.query_one(GameLine)
        rendered = game_line.render().plain.splitlines()

        assert len(rendered) == 45
        assert rendered[5][3] == "┌"
        assert rendered[5][67] == "┌"
        assert rendered[30][67] == "┌"
        assert [
            (region.row, region.start, region.end, region.token, region.enabled, region.title)
            for region in game_line.regions
        ] == [(7, 5, 17, GUILD_TASK_TOKEN, True, "选择任务")]
        assert await pilot.hover(game_line, offset=(6, 7))
        assert game_line.tooltip == "选择任务"
        assert game_line.hovered_region == 0
        assert await pilot.click(game_line, offset=(6, 7))
        assert ("activate", GUILD_TASK_TOKEN) in worker.commands
        assert not game_line.interactions_enabled
        assert await pilot.hover(game_line, offset=(6, 7))
        assert game_line.hovered_region is None
        assert game_line.tooltip is None


async def test_semantic_separator_tracks_the_viewport_without_wrapping_plain_text(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    separator = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        (),
        (SeparatorLayout(0, "～"),),
    )
    async with app.run_test(size=(61, 30)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([separator])
        await pilot.pause()
        game_line = app.query_one(GameLine)

        assert cell_len(game_line.render().plain) == viewport.content_width == 59
        assert "\n" not in game_line.render().plain

        await pilot.resize_terminal(37, 30)
        assert cell_len(game_line.render().plain) == viewport.content_width == 35
        assert not viewport.show_horizontal_scrollbar


async def test_runtime_text_columns_control_terminal_advance(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    line = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        (
            DisplaySegment("A", logical_columns=1),
            DisplaySegment(" ", logical_columns=0),
            DisplaySegment("B", logical_columns=1),
            DisplaySegment("■", logical_columns=2),
            DisplaySegment("☀", logical_columns=2),
            DisplaySegment("❤", logical_columns=2),
            DisplaySegment("- ", logical_columns=2),
        ),
    )
    bar_line = DisplayLineModel(
        2,
        False,
        True,
        True,
        0,
        (DisplaySegment("▅" * 64, logical_columns=64), DisplaySegment("│", logical_columns=2)),
    )
    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([line, bar_line])
        await pilot.pause()
        rendered, rendered_bar = [item.render().plain for item in app.query(GameLine)]

        assert rendered == "AB■ ☀ ❤ - "
        assert rendered[:2] == "AB"
        assert cell_len(rendered) == 10
        assert rendered_bar == "▅" * 64 + "│ "
        assert rendered_bar.index("│") == 64


def test_runtime_wide_box_cells_only_continue_right_facing_strokes() -> None:
    def render(character: str) -> str:
        return terminal_segment_text(DisplaySegment(character, logical_columns=2))

    assert [render(character) for character in ("┌", "┏", "╔")] == ["┌─", "┏━", "╔═"]
    assert [render(character) for character in ("│", "┐", "╱")] == ["│ ", "┐ ", "╱ "]


async def test_tagged_html_table_edges_stay_continuous_and_aligned(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    def html_text(text: str) -> list[object]:
        return variant(0, text, 0, len(text))

    def html_button(text: str, token_id: int) -> list[object]:
        return variant(
            1,
            7,
            [],
            [html_text(text)],
            {0: 8, 1: token_id, 4: 3, 5: True},
            0,
            0,
            variant(4, str(token_id), text, None),
        )

    def html_line(line_id: int, nodes: list[object]) -> DisplayLineModel:
        return parse_line(
            {
                0: line_id,
                1: False,
                2: True,
                3: True,
                4: 0,
                5: [variant(2, {0: nodes})],
            }
        )

    def visible_columns(row: str, character: str) -> list[int]:
        return [cell_len(row[:index]) for index, value in enumerate(row) if value == character]

    role_token = {0: 8, 1: 21}
    page_token = {0: 8, 1: 22}
    content_line = html_line(
        1,
        [
            html_text("│"),
            html_button("角色", role_token[1]),
            html_text("        ││"),
            html_button("友人", 23),
            html_text("│"),
        ],
    )
    footer_line = html_line(
        2,
        [
            html_text("└──"),
            html_button(" ◀页码▶ ", page_token[1]),
            html_text("┘└─"),
            html_button("▶ ", 24),
            html_text("┘"),
        ],
    )

    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([content_line, footer_line])
        await pilot.pause()
        content, footer = [item.render().plain for item in app.query(GameLine)]

        assert footer.startswith("└─────")
        assert visible_columns(content, "│") == [0, 14, 16, 22]
        assert visible_columns(footer, "┘") == [14, 22]
        game_lines = list(app.query(GameLine))
        assert any(region.token == role_token for region in game_lines[0].regions)
        assert any(region.token == page_token for region in game_lines[-1].regions)


async def test_mismatched_html_box_rows_align_across_terminal_widths(
    tmp_path: Path,
) -> None:
    def html_line(line_id: int, text: str) -> DisplayLineModel:
        return parse_line(
            {
                0: line_id,
                1: False,
                2: True,
                3: True,
                4: 0,
                5: [variant(2, {0: [variant(0, text, 0, len(text))]})],
            }
        )

    lines = [
        html_line(1, f"┌烙印{'─' * 62}┐"),
        html_line(2, f"│请选择要提升的能力{' ' * 104}│"),
        html_line(3, f"└{'─' * 64}┘"),
    ]

    for width in (80, 132, 200):
        app = RustyEraTui(tmp_path, None)
        app.worker = FakeWorker()  # type: ignore[assignment]
        async with app.run_test(size=(width, 20)) as pilot:
            viewport = app.query_one(GameViewport)
            await viewport.set_lines(lines)
            await pilot.pause()
            rendered = [item.render().plain for item in app.query(GameLine)]

            right_edges = [
                cell_len(row[: row.rfind(character)])
                for row, character in zip(rendered, ("┐", "│", "┘"), strict=True)
            ]
            assert right_edges == [130, 130, 130]


def test_html_box_projection_preserves_edge_interaction_and_extends_bottom_stroke() -> None:
    token = {0: 8, 1: 77}
    top = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        (
            DisplaySegment("┌", logical_columns=2),
            DisplaySegment("────────", logical_columns=16),
            DisplaySegment("┐", logical_columns=2),
        ),
    )
    edge = DisplaySegment(
        "│",
        token=token,
        enabled=False,
        title="edge",
        generation=4,
        logical_columns=2,
        interaction_sequence=9,
    )
    interior = DisplayLineModel(
        2,
        False,
        True,
        True,
        0,
        (DisplaySegment("│", logical_columns=2), DisplaySegment("能力"), edge),
    )
    bottom = DisplayLineModel(
        3,
        False,
        True,
        True,
        0,
        (
            DisplaySegment("└", logical_columns=2),
            DisplaySegment("──", logical_columns=4),
            replace(edge, text="┘", token=None),
        ),
    )

    projected, states = project_html_box_rows([top, interior, bottom])
    inserted = projected[1].segments[-2]
    assert replace(inserted, text=edge.text, logical_columns=edge.logical_columns) == edge
    assert projected[2].segments[-2].text == "─" * 12
    assert states == [None, 20, 20, None]


async def test_appended_html_box_rows_inherit_cached_top_width(tmp_path: Path) -> None:
    def html_line(line_id: int, text: str) -> DisplayLineModel:
        return parse_line(
            {
                0: line_id,
                1: False,
                2: True,
                3: True,
                4: 0,
                5: [variant(2, {0: [variant(0, text, 0, len(text))]})],
            }
        )

    top = html_line(1, "┌────────┐")
    interior = html_line(2, "│能力│")
    bottom = html_line(3, "└──┘")
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([top])
        await viewport.set_lines([top, interior, bottom], changed_from=1)
        await pilot.pause()
        rendered = [item.render().plain for item in app.query(GameLine)]
        right_edges = [
            cell_len(row[: row.rfind(character)])
            for row, character in zip(rendered, ("┐", "│", "┘"), strict=True)
        ]
        assert right_edges == [18, 18, 18]


async def test_appending_after_large_history_projects_only_the_new_suffix(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    lines = [
        DisplayLineModel(
            index,
            False,
            True,
            True,
            0,
            (DisplaySegment("", right_edge=index > 0),),
        )
        for index in range(5_000)
    ]
    projected_lengths: list[int] = []
    original = widgets._project_html_box_rows

    def counted(
        suffix: list[DisplayLineModel], active_columns: int | None = None
    ) -> tuple[list[DisplayLineModel], list[int | None]]:
        projected_lengths.append(len(suffix))
        return original(suffix, active_columns)

    monkeypatch.setattr(widgets, "_project_html_box_rows", counted)
    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines)
        await pilot.pause()
        projected_lengths.clear()
        appended = DisplayLineModel(
            5_000, False, True, True, 0, (DisplaySegment("ordinary output"),)
        )
        await viewport.set_lines([*lines, appended], changed_from=5_000)
        assert projected_lengths == [1]


async def test_prefix_trim_reuses_retained_widgets_and_projects_only_the_new_suffix(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    lines = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
        for index in range(200)
    ]
    projected_lengths: list[int] = []
    original = widgets._project_html_box_rows

    def counted(
        suffix: list[DisplayLineModel], active_columns: int | None = None
    ) -> tuple[list[DisplayLineModel], list[int | None]]:
        projected_lengths.append(len(suffix))
        return original(suffix, active_columns)

    monkeypatch.setattr(widgets, "_project_html_box_rows", counted)
    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines)
        await pilot.pause()
        retained_widgets = list(viewport.children)[1:]
        projected_lengths.clear()
        appended = DisplayLineModel(200, False, True, True, 0, (DisplaySegment("appended"),))

        await viewport.set_lines(
            [*lines[1:], appended], changed_from=len(lines) - 1, trimmed_prefix=1
        )

        assert projected_lengths == [1]
        assert list(viewport.children)[:-1] == retained_widgets
        assert [line.line_id for line in viewport.models] == list(range(1, 201))


async def test_prefix_trim_preserves_html_box_projection_state(tmp_path: Path) -> None:
    def html_line(line_id: int, text: str) -> DisplayLineModel:
        return parse_line(
            {
                0: line_id,
                1: False,
                2: True,
                3: True,
                4: 0,
                5: [variant(2, {0: [variant(0, text, 0, len(text))]})],
            }
        )

    top = html_line(1, "┌────────┐")
    interior = html_line(2, "│能力│")
    bottom = html_line(3, "└──┘")
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([top, interior])
        await viewport.set_lines([interior, bottom], changed_from=1, trimmed_prefix=1)
        await pilot.pause()

        rendered = [item.render().plain for item in app.query(GameLine)]
        right_edges = [
            cell_len(row[: row.rfind(character)])
            for row, character in zip(rendered, ("│", "┘"), strict=True)
        ]
        assert right_edges == [18, 18]


async def test_prefix_trim_rebuilds_box_state_when_trim_exceeds_the_cached_history(
    tmp_path: Path,
) -> None:
    def html_line(line_id: int, text: str) -> DisplayLineModel:
        return parse_line(
            {
                0: line_id,
                1: False,
                2: True,
                3: True,
                4: 0,
                5: [variant(2, {0: [variant(0, text, 0, len(text))]})],
            }
        )

    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    top = html_line(1, "┌────────┐")
    bottom = html_line(3, "└──┘")

    async with app.run_test(size=(40, 20)):
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([top])
        # The runtime also appended and trimmed an ordinary line before the next UI commit. The
        # missing row breaks the box, so the retained bottom must not inherit the cached top width.
        await viewport.set_lines([bottom], changed_from=0, trimmed_prefix=2)

        rendered = app.query_one(GameLine).render().plain
        assert cell_len(rendered[: rendered.rfind("┘")]) == 6


async def test_runtime_page_navigation_triangles_keep_the_footer_corner_aligned(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    def text_runs(text: str) -> list[list[object]]:
        return [
            variant(
                8,
                character,
                None,
                None,
                2 if "\u2500" <= character <= "\u257f" or character in "页码" else 1,
            )
            for character in text
        ]

    def button(text: str, token_id: int) -> list[object]:
        return variant(1, text_runs(text), {0: 8, 1: token_id}, None, None, None, 0, True)

    content = parse_line(
        {
            0: 1,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [*text_runs("│"), variant(8, " " * 78, None, None, 78), *text_runs("│")],
        }
    )
    footer = parse_line(
        {
            0: 2,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [
                *text_runs("└" + "─" * 28),
                button(" [--] ◀", 21_001),
                *text_runs(" 页码. 1 "),
                button("▶ [++]", 21_002),
                *text_runs("┘"),
            ],
        }
    )

    async with app.run_test(size=(90, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([content, footer])
        await pilot.pause()
        content_text, footer_text = [item.render().plain for item in app.query(GameLine)]

        assert [
            cell_len(content_text[:index])
            for index, value in enumerate(content_text)
            if value == "│"
        ] == [0, 80]
        assert [
            cell_len(footer_text[:index]) for index, value in enumerate(footer_text) if value == "┘"
        ] == [80]


async def test_full_width_space_replacement_hotly_rerenders_existing_and_new_lines(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    first = DisplayLineModel(1, False, True, True, 0, (DisplaySegment("A　B"),))
    second = DisplayLineModel(2, False, True, True, 0, (DisplaySegment("C　D"),))
    async with app.run_test(size=(40, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([first])
        viewport.set_replace_full_width_spaces(True)
        await pilot.pause()
        assert app.query_one(GameLine).render().plain == "A  B"
        assert first.segments[0].text == "A　B"

        await viewport.set_lines([first, second])
        await pilot.pause()
        assert [line.render().plain for line in app.query(GameLine)] == ["A  B", "C  D"]


async def test_vertical_scrollbar_gutter_does_not_create_transient_horizontal_overflow(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    history = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
        for index in range(44)
    ]
    semantic_separator = DisplayLineModel(
        44,
        False,
        True,
        True,
        0,
        (),
        (SeparatorLayout(0, "-"),),
    )
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        separator = app.query_one("#separator-line")
        with app.batch_update():
            overflow = await viewport.set_lines([*history, semantic_separator])
            separator.display = not overflow

        for _ in range(3):
            await pilot.pause()
            rendered_separator = list(app.query(GameLine))[-1]
            assert viewport.content_width == 98
            assert cell_len(rendered_separator.render().plain) == 98
            assert viewport.virtual_size.width == 98
            assert viewport.max_scroll_x == 0
            assert not viewport.show_horizontal_scrollbar
            assert separator.display

        app._send_viewport_projection()
        projection = next(
            value for kind, value in reversed(worker.commands) if kind == "projection"
        )
        assert projection[:2] == viewport.content_dimensions
        assert projection[0] == 98


async def test_viewport_follows_appended_history(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    lines = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
        for index in range(40)
    ]
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines)
        await pilot.pause()
        await pilot.pause()
        assert viewport.is_vertical_scroll_end

        viewport.scroll_home(animate=False, x_axis=False)
        await pilot.pause()
        await pilot.pause()
        assert not viewport.is_vertical_scroll_end

        await viewport.set_lines([*lines, DisplayLineModel(40, False, True, True, 0, ())])
        await pilot.pause()
        await pilot.pause()
        assert viewport.is_vertical_scroll_end


async def test_nf_viewport_preserves_upscroll_until_an_ordinary_wait(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    lines = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
        for index in range(60)
    ]
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines)
        await pilot.pause()
        viewport.scroll_home(animate=False, x_axis=False)
        await pilot.pause()
        viewport.observe_input_viewport_policy(1)

        await viewport.set_lines([*lines, DisplayLineModel(60, False, True, True, 0, ())])
        await pilot.pause()
        assert not viewport.is_vertical_scroll_end

        # A closed wait and a subsequent NF wait retain the same intent. A
        # CLEARLINE-style prefix trim must not silently restore following.
        viewport.observe_input_viewport_policy(None)
        viewport.observe_input_viewport_policy(1)
        retained = lines[10:]
        await viewport.set_lines(retained, changed_from=0, trimmed_prefix=10)
        await pilot.pause()
        assert not viewport.is_vertical_scroll_end

        viewport.observe_input_viewport_policy(0)
        await pilot.pause()
        assert viewport.is_vertical_scroll_end


async def test_viewport_preserves_scroll_for_equal_length_dynamic_tail_refresh(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    lines = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
        for index in range(40)
    ]
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines)
        await pilot.pause()
        await pilot.pause()
        viewport.scroll_to(y=5, animate=False)
        await pilot.pause()
        before = viewport.scroll_y
        tail_widget = list(viewport.children)[-1]

        replacement = DisplayLineModel(
            100,
            False,
            True,
            True,
            0,
            (DisplaySegment("animated frame"),),
        )
        await viewport.set_lines([*lines[:-1], replacement], changed_from=len(lines) - 1)
        await pilot.pause()
        await pilot.pause()

        assert viewport.scroll_y == before
        assert viewport.models[-1] == replacement
        assert list(viewport.children)[-1] is tail_widget


async def test_dynamic_tail_refresh_only_reprojects_the_changed_history(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    lines = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
        for index in range(200)
    ]
    projected: list[int] = []
    original = widgets._projected_line_width

    def counted(line: DisplayLineModel, width: int) -> int:
        projected.append(line.line_id)
        return original(line, width)

    monkeypatch.setattr(widgets, "_projected_line_width", counted)
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines)
        await pilot.pause()
        projected.clear()

        replacement = DisplayLineModel(
            999, False, True, True, 0, (DisplaySegment("animated frame"),)
        )
        await viewport.set_lines([*lines[:-1], replacement], changed_from=len(lines) - 1)

        assert projected == [replacement.line_id]
        projected.clear()
        appended = DisplayLineModel(
            1_000, False, True, True, 0, (DisplaySegment("ordinary output"),)
        )
        await viewport.set_lines([*lines[:-1], replacement, appended], changed_from=len(lines))

        assert projected == [appended.line_id]


async def test_interaction_lock_only_rerenders_interactive_history(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    plain = [
        DisplayLineModel(index, False, True, True, 0, (DisplaySegment("history"),))
        for index in range(200)
    ]
    button = DisplayLineModel(
        201,
        False,
        True,
        True,
        0,
        (DisplaySegment("button", token={0: 1, 1: 1}, interaction_sequence=1),),
    )
    rendered: list[int] = []
    original = GameLine._render_line

    def counted(widget: GameLine) -> None:
        rendered.append(widget.line.line_id)
        original(widget)

    monkeypatch.setattr(GameLine, "_render_line", counted)
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([*plain, button])
        await pilot.pause()
        rendered.clear()

        viewport.disable_interactions()

        assert rendered == [button.line_id]


async def test_interaction_policy_only_rerenders_real_enabled_transitions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    def interaction(line_id: int, generation: int, sequence: int) -> DisplayLineModel:
        return DisplayLineModel(
            line_id,
            False,
            True,
            True,
            0,
            (
                DisplaySegment(
                    f"button {line_id}",
                    token={0: 1, 1: line_id},
                    generation=generation,
                    interaction_sequence=sequence,
                ),
            ),
        )

    stale = [interaction(index, 0, index) for index in range(1, 501)]
    current = [interaction(501, 1, 501), interaction(502, 1, 502)]
    future = [interaction(503, 2, 503), interaction(504, 2, 504)]
    lines = [*stale, *current, *future]
    rendered: list[int] = []
    original = GameLine._render_line

    def counted(widget: GameLine) -> None:
        rendered.append(widget.line.line_id)
        original(widget)

    monkeypatch.setattr(GameLine, "_render_line", counted)
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(lines, button_generation=1)
        await pilot.pause()
        rendered.clear()

        await viewport.set_lines(
            lines,
            changed_from=len(lines),
            button_generation=1,
            retired_interaction_sequence=502,
        )
        assert sorted(rendered) == [501, 502]
        rendered.clear()

        await viewport.set_lines(
            lines,
            changed_from=len(lines),
            button_generation=2,
            retired_interaction_sequence=502,
        )
        assert sorted(rendered) == [503, 504]
        rendered.clear()

        await viewport.set_lines(
            lines,
            changed_from=len(lines),
            button_generation=2,
            retired_interaction_sequence=504,
        )
        assert sorted(rendered) == [503, 504]
        rendered.clear()

        await viewport.set_lines(
            lines,
            changed_from=len(lines),
            button_generation=2,
            retired_interaction_sequence=502,
        )
        assert sorted(rendered) == [503, 504]


async def test_interaction_cache_drops_an_interactive_to_plain_replacement(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    button = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        (DisplaySegment("button", token={0: 1, 1: 1}, generation=1, interaction_sequence=1),),
    )
    plain = DisplayLineModel(2, False, True, True, 0, (DisplaySegment("plain"),))
    rendered: list[int] = []
    original = GameLine._render_line

    def counted(widget: GameLine) -> None:
        rendered.append(widget.line.line_id)
        original(widget)

    monkeypatch.setattr(GameLine, "_render_line", counted)
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([button], button_generation=1)
        await pilot.pause()
        await viewport.set_lines([plain], changed_from=0, button_generation=1)
        rendered.clear()

        viewport.disable_interactions()

        assert rendered == []
        assert viewport._interactive_children == set()
        assert viewport._enabled_interaction_children == set()
        assert viewport._interaction_children_by_generation == {}


async def test_interaction_cache_removes_deleted_children_and_clears_full_rebuild(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    def button(line_id: int, generation: int) -> DisplayLineModel:
        return DisplayLineModel(
            line_id,
            False,
            True,
            True,
            0,
            (
                DisplaySegment(
                    "button",
                    token={0: 1, 1: line_id},
                    generation=generation,
                    interaction_sequence=line_id,
                ),
            ),
        )

    first = button(1, 1)
    deleted = button(2, 2)
    replacement = button(3, 3)
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([first, deleted], button_generation=1)
        await pilot.pause()
        deleted_widget = list(viewport.children)[1]

        await viewport.set_lines([first], changed_from=1, button_generation=1)

        assert deleted_widget not in viewport._interactive_children
        assert deleted_widget not in viewport._interaction_children_by_generation.get(2, set())

        old_widgets = set(viewport.children)
        await viewport.set_lines(
            [replacement, DisplayLineModel(4, False, True, True, 0, ())],
            button_generation=3,
        )

        assert old_widgets.isdisjoint(viewport._interactive_children)
        assert viewport._interactive_children == {list(viewport.children)[0]}
        assert set(viewport._interaction_children_by_generation) == {3}


async def test_ordinary_append_stays_incremental_after_historical_right_edge_content(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    source = [
        DisplayLineModel(1, False, True, True, 0, (DisplaySegment("save slot"),)),
        DisplayLineModel(2, False, True, True, 0, (DisplaySegment("delete", right_edge=True),)),
        *(
            DisplayLineModel(index, False, True, True, 0, (DisplaySegment(f"line {index}"),))
            for index in range(3, 203)
        ),
    ]
    projected: list[int] = []
    full_merges = 0
    original_width = widgets._projected_line_width
    original_merge = widgets._merge_save_delete_lines_with_prefixes

    def counted_width(line: DisplayLineModel, width: int) -> int:
        projected.append(line.line_id)
        return original_width(line, width)

    def counted_merge(
        lines: list[DisplayLineModel],
    ) -> tuple[list[DisplayLineModel], list[int]]:
        nonlocal full_merges
        full_merges += 1
        return original_merge(lines)

    monkeypatch.setattr(widgets, "_projected_line_width", counted_width)
    monkeypatch.setattr(widgets, "_merge_save_delete_lines_with_prefixes", counted_merge)
    async with app.run_test(size=(100, 20)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines(source)
        await pilot.pause()
        projected.clear()
        full_merges = 0
        appended = DisplayLineModel(999, False, True, True, 0, (DisplaySegment("ordinary output"),))

        await viewport.set_lines([*source, appended], changed_from=len(source))

        assert projected == [appended.line_id]
        assert full_merges == 0


async def test_multiline_button_regions_merge_padding_and_use_row_coordinates(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    first = {0: 3, 1: 1}
    second = {0: 3, 1: 2}
    line = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        (
            DisplaySegment("   ", token=first, title="first"),
            DisplaySegment("body\n", token=first, title="first"),
            DisplaySegment("next", token=second, title="second"),
        ),
    )
    app.presentation.lines = [line]
    app.active_wait = {0: 4, 1: 2}
    async with app.run_test(size=(100, 30)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([line])
        await pilot.pause()
        game_line = app.query_one(GameLine)

        assert len(game_line.regions) == 2
        assert game_line.regions[0].row == 0
        assert (game_line.regions[0].start, game_line.regions[0].end) == (0, 7)
        assert game_line.regions[1].row == 1
        assert game_line._region_at(1, 0) == 0
        assert game_line._region_at(1, 1) == 1
        assert await pilot.click(".game-line", offset=(1, 1))
        assert ("activate", second) in worker.commands


async def test_save_delete_action_is_merged_at_the_slot_row_right_edge(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    slot_token = {0: 4, 1: 1}
    delete_token = {0: 4, 1: 2}
    slot = DisplayLineModel(
        1,
        False,
        True,
        True,
        0,
        (DisplaySegment("[ 1] Save", token=slot_token),),
    )
    delete = DisplayLineModel(
        2,
        False,
        True,
        True,
        0,
        (
            DisplaySegment(
                "[X]",
                token=delete_token,
                title="Delete save01.sav",
                right_edge=True,
            ),
        ),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([slot, delete])
        await pilot.pause()

        lines = list(app.query(GameLine))
        assert len(lines) == 1
        game_line = lines[0]
        assert len(game_line.regions) == 2
        assert game_line.regions[1].token == delete_token
        assert game_line.regions[1].end == game_line.size.width
        assert str(game_line.render()).endswith("[X]")
