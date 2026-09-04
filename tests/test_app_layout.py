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


def _html_text_line(line_id: int, text: str) -> DisplayLineModel:
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
    lines = [
        _html_text_line(1, f"┌烙印{'─' * 62}┐"),
        _html_text_line(2, f"│请选择要提升的能力{' ' * 104}│"),
        _html_text_line(3, f"└{'─' * 64}┘"),
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
    top = _html_text_line(1, "┌────────┐")
    interior = _html_text_line(2, "│能力│")
    bottom = _html_text_line(3, "└──┘")
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
    top = _html_text_line(1, "┌────────┐")
    interior = _html_text_line(2, "│能力│")
    bottom = _html_text_line(3, "└──┘")
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
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    top = _html_text_line(1, "┌────────┐")
    bottom = _html_text_line(3, "└──┘")

    async with app.run_test(size=(40, 20)):
        viewport = app.query_one(GameViewport)
        await viewport.set_lines([top])
        # The runtime also appended and trimmed an ordinary line before the next UI commit. The
        # missing row breaks the box, so the retained bottom must not inherit the cached top width.
        await viewport.set_lines([bottom], changed_from=0, trimmed_prefix=2)

        rendered = app.query_one(GameLine).render().plain
        assert cell_len(rendered[: rendered.rfind("┘")]) == 6
