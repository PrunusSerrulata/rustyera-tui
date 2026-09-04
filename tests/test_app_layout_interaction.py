from __future__ import annotations

from pytest import MonkeyPatch

from app_test_support import (
    DisplayLineModel,
    DisplaySegment,
    FakeWorker,
    GameLine,
    GameViewport,
    Path,
    RustyEraTui,
    SeparatorLayout,
    cell_len,
    parse_line,
    variant,
)
from rustyera_tui import widgets


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
