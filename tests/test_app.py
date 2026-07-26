from __future__ import annotations

import queue
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from textual.widgets import DataTable, Input, RichLog, Select, Static

from rustyera_tui.app import RustyEraTui
from rustyera_tui.dialogs import FatalErrorDialog
from rustyera_tui.log_model import LogLevel
from rustyera_tui.presentation import (
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    SeparatorLayout,
)
from rustyera_tui.runtime import FrontendEvent, RuntimeFailure
from rustyera_tui.widgets import GameLine, GameViewport
from rustyera_tui.wire import variant


class FakeWorker:
    def __init__(self) -> None:
        self.events: queue.Queue[Any] = queue.Queue()
        self.commands: list[tuple[str, Any]] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.started

    def send(self, kind: str, value: Any = None) -> None:
        self.commands.append((kind, value))

    def stop(self) -> None:
        self.started = False

    def join(self, timeout: float | None = None) -> None:
        del timeout


async def test_menu_hover_click_and_debug_gating(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.click("#menu-file")
        assert app.query_one("#file-menu").has_class("visible")
        await pilot.click("#file-reload-all")
        assert ("reload_all", None) in worker.commands

        await pilot.click("#menu-debug")
        assert app.query_one("#debug-console").disabled
        await pilot.click("#debug-toggle")
        assert ("debug_enable", None) in worker.commands

        assert not app.query("#menu-line-number")
        assert str(app.query_one("#prompt-label").render()) == "> "
        separator = app.query_one("#separator-line")
        prompt_row = app.query_one("#prompt-row")
        assert separator.size.width == app.size.width
        assert separator.region.bottom == prompt_row.region.y
        assert app.query_one("#file-restart").styles.content_align[0] == "left"
        assert app.query_one("#menu-file").styles.content_align[0] == "left"


async def test_prompt_submits_through_worker(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt")
        prompt.disabled = False
        prompt.focus()
        await pilot.press("1", "2", "enter")
        assert ("submit_text", "12") in worker.commands


async def test_viewport_click_submits_only_a_plain_enter_wait(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app.active_wait = {0: 7, 1: 0}
        await pilot.click("#game-viewport", offset=(10, 5))
        assert ("submit_text", "") in worker.commands

        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        assert ("skip_enter_waits", None) in worker.commands

        worker.commands.clear()
        app.active_wait = {0: 8, 1: 2}
        await pilot.click("#game-viewport", offset=(10, 5))
        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        assert ("submit_text", "") not in worker.commands
        assert ("skip_enter_waits", None) not in worker.commands


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
        assert [cell_len(row) for row in rows] == [100, 60]

        await pilot.resize_terminal(80, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [80, 48]

        await pilot.resize_terminal(79, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [76, 76]

        await pilot.resize_terminal(59, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [57, 57, 38]
        assert id(app.query_one(GameLine)) == identity

        await pilot.resize_terminal(24, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [24] * 8

        await pilot.resize_terminal(15, 30)
        rows = game_line.render().plain.splitlines()
        assert [cell_len(row) for row in rows] == [16] * 8
        assert viewport.show_horizontal_scrollbar

        await pilot.resize_terminal(120, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [120, 72]

        await pilot.resize_terminal(121, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [120, 72]

        await pilot.resize_terminal(143, 30)
        assert [cell_len(row) for row in game_line.render().plain.splitlines()] == [120, 72]

        await pilot.resize_terminal(144, 30)
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

        assert cell_len(game_line.render().plain) == viewport.size.width == 61
        assert "\n" not in game_line.render().plain

        await pilot.resize_terminal(37, 30)
        assert cell_len(game_line.render().plain) == viewport.size.width == 37
        assert not viewport.show_horizontal_scrollbar


async def test_viewport_update_always_scrolls_to_the_bottom(tmp_path: Path) -> None:
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


async def test_log_dialog_filters_entries_at_the_selected_threshold(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    app._log("debug message", LogLevel.DEBUG)
    app._log("info message", LogLevel.INFO)
    app._log("warning message", LogLevel.WARNING)
    app._log("error message", LogLevel.ERROR)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.click("#menu-debug")
        await pilot.click("#debug-logs")
        view = app.screen.query_one("#log-view", RichLog)
        level = app.screen.query_one("#log-level", Select)
        await pilot.pause()
        assert level.value == "info"
        assert len(view.lines) == 3

        level.value = "warning"
        await pilot.pause()
        assert len(view.lines) == 2

        level.value = "debug"
        await pilot.pause()
        assert len(view.lines) == 4
        assert view.is_vertical_scroll_end


async def test_runtime_fault_remains_visible_in_the_prompt(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    failure = RuntimeFailure(
        code=3,
        message="place storage is unavailable",
        function="EVENTTRAIN",
        source_path="BEFORETRAIN.ERB",
        source_line=28,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("runtime_fault", failure))
        await pilot.pause(0.1)
        prompt = app.query_one("#prompt", Input)
        assert prompt.disabled
        assert app.blocking_error == failure.display()
        assert prompt.placeholder == failure.display()
        assert app.query_one("#prompt-label").styles.color.hex6 == "#EF4444"


async def test_prompt_color_tracks_runtime_and_input_state(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)):
        label = app.query_one("#prompt-label")
        assert label.has_class("prompt-running")
        assert label.styles.color.hex6 == "#A9A9A9"

        app._toggle_prompt_blink()
        assert label.has_class("prompt-running-bright")
        assert label.styles.color.hex6 == "#F8FAFC"
        app._toggle_prompt_blink()
        assert label.styles.color.hex6 == "#A9A9A9"

        app.active_wait = {0: 1, 1: 2}
        app._update_prompt()
        assert label.has_class("prompt-number")
        assert label.styles.color.hex6 == "#F8FAFC"

        app.active_wait = {0: 2, 1: 0}
        app._update_prompt()
        assert label.has_class("prompt-enter")
        assert label.styles.color.hex6 == "#7FFFD4"

        app.active_wait = {0: 3, 1: 3}
        app._update_prompt()
        assert label.has_class("prompt-other")
        assert label.styles.color.hex6 == "#800080"


async def test_log_uses_text_for_runtime_and_debug_enums(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("phase", 5))
        worker.events.put(FrontendEvent("debug_stopped", {1: [2, []]}))
        await pilot.pause(0.1)
        phase_log = next(log for log in app.logs if "Runtime phase -> WaitingInput" in log)
        pause_log = next(log for log in app.logs if "调试暂停：StepCompleted" in log)
        assert phase_log.level is LogLevel.DEBUG
        assert pause_log.level is LogLevel.DEBUG


async def test_single_step_prompt_shows_source_and_f10_advances(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app._set_debug_enabled(True)
        app._debug_action("debug-step-toggle")
        worker.commands.clear()

        worker.events.put(
            FrontendEvent(
                "debug_stopped",
                {
                    0: {0: 1, 1: 2, 2: 3, 3: 4},
                    1: variant(2),
                    2: 7,
                    3: {0: "ERB/main.erb", 4: 12},
                },
            )
        )
        await pilot.pause(0.1)

        prompt = app.query_one("#prompt", Input)
        assert prompt.disabled
        assert prompt.placeholder == "单步暂停：ERB/main.erb:12（F10 继续）"

        await pilot.press("f10")
        assert ("debug_step", None) in worker.commands
        assert not app.debug_paused


async def test_debug_host_wait_blocks_input_only_until_runtime_resumes(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    wait = {0: 7, 1: 0, 11: {0: 1, 1: 2}}
    async with app.run_test(size=(100, 30)) as pilot:
        app._set_debug_enabled(True)
        app.single_step = True
        worker.events.put(FrontendEvent("wait", wait))
        worker.events.put(
            FrontendEvent(
                "debug_stopped",
                {0: {0: 1}, 1: variant(3), 2: 7, 3: {0: "ERB/main.erb", 4: 20}},
            )
        )
        await pilot.pause(0.1)

        await pilot.click("#game-viewport", offset=(10, 5))
        assert not any(kind == "submit_text" for kind, _value in worker.commands)

        worker.events.put(FrontendEvent("phase", 5))
        await pilot.pause(0.1)
        await pilot.click("#game-viewport", offset=(10, 5))
        assert ("submit_text", "") in worker.commands
        assert not any("命令被拒绝" in log for log in app.logs)


async def test_debug_console_commands_remain_available_while_paused(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)):
        app._set_debug_enabled(True)
        app._debug_action("debug-console")
        assert app.console_dialog is not None
        app.debug_paused = True

        app.on_debug_console_dialog_submitted(
            app.console_dialog.Submitted("RESULT = 7", execute=True)
        )

        assert ("debug_action", ("console_execute", "RESULT = 7")) in worker.commands


async def test_variable_viewer_renders_selected_variable_value(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    descriptor = {0: b"key", 1: "RESULT", 2: 0, 3: 0, 4: [10], 5: True}
    async with app.run_test(size=(100, 30)) as pilot:
        app._set_debug_enabled(True)
        app._debug_action("debug-variables")
        await pilot.pause()
        assert app.variable_dialog is not None
        app.variable_dialog.set_variables({1: [descriptor]})
        app.debug_paused = True

        table = app.screen.query_one("#variable-table", DataTable)
        table.move_cursor(row=0)
        await pilot.press("enter")
        assert ("debug_action", ("read_variable", descriptor)) in worker.commands

        app._handle_debug_response(("variable_value", 2, [{0: {6: [0]}, 1: variant(0, 42), 2: 9}]))
        assert table.get_row_at(0)[5] == "42"
        assert "当前值[0]：42" in str(app.screen.query_one("#variable-status", Static).render())


async def test_stack_viewer_mounts_before_requesting_and_pages_to_the_live_fiber(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app._set_debug_enabled(True)
        app._debug_action("debug-stack")
        await pilot.pause()
        assert ("debug_action", ("fibers", None)) in worker.commands

        worker.commands.clear()
        app._handle_debug_response(
            (
                "fibers",
                5,
                [{1: [{0: 1, 1: 3, 2: False, 3: 0}], 2: 1024}],
            )
        )
        assert worker.commands == [("debug_action", ("fibers", 1024))]

        worker.commands.clear()
        app._handle_debug_response(
            (
                "fibers",
                5,
                [{1: [{0: 2048, 1: 6, 2: True, 3: 2}]}],
            )
        )
        assert worker.commands == [("debug_action", ("call_stack", 2048))]

        worker.commands.clear()
        app._handle_debug_response(
            (
                "call_stack",
                6,
                [
                    {
                        1: 2048,
                        2: [
                            {
                                0: 99,
                                3: "EVENTTRAIN",
                                4: 17,
                                5: {0: "ERB/TRAIN.ERB", 4: 42},
                            }
                        ],
                    }
                ],
            )
        )
        assert worker.commands == []
        frame_table = app.screen.query_one("#frame-table", DataTable)
        assert frame_table.get_row_at(0)[1] == "EVENTTRAIN"
        assert not app.screen.query("#operand-table")


async def test_stack_viewer_fiber_height_depends_only_on_window_size(
    tmp_path: Path,
) -> None:
    for screen_height in (30, 60):
        app = RustyEraTui(tmp_path, None)
        worker = FakeWorker()
        app.worker = worker  # type: ignore[assignment]
        async with app.run_test(size=(100, screen_height)) as pilot:
            app._set_debug_enabled(True)
            app._debug_action("debug-stack")
            await pilot.pause()

            dialog = app.stack_dialog
            assert dialog is not None
            assert str(dialog.query_one(".dialog-title").render()) == "纤程与调用栈"
            fiber_table = dialog.query_one("#fiber-table", DataTable)
            frame_table = dialog.query_one("#frame-table", DataTable)
            empty_height = fiber_table.size.height
            assert empty_height <= 11

            dialog.set_fibers({1: []})
            assert "当前无活动纤程" in str(dialog.query_one("#stack-status").render())
            dialog.set_fibers({1: [{0: 1, 1: 0, 2: True, 3: 1}]})
            await pilot.pause()
            assert fiber_table.size.height == empty_height
            dialog.set_fibers(
                {1: [{0: fiber_id, 1: 0, 2: False, 3: 1} for fiber_id in range(2, 10)]}
            )
            await pilot.pause()
            assert fiber_table.size.height == empty_height
            assert frame_table.size.height > fiber_table.size.height


async def test_inline_button_hover_and_click_submits_opaque_token(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    style = {
        0: {0: 0, 1: 255, 2: 128, 3: 255},
        2: False,
        3: False,
        4: False,
        5: False,
        7: 12_000,
    }
    token = {0: 4, 1: 9}
    button = variant(
        1,
        [variant(0, "开始", style, None)],
        token,
        "开始游戏",
        None,
        variant(0, 0),
        0,
        True,
    )
    line = {0: 1, 1: False, 2: True, 3: True, 4: 0, 5: [button]}
    settings = {
        0: 100_000,
        1: 1_000,
        2: {0: 0, 1: 0, 2: 0, 3: 255},
        3: {0: 255, 1: 255, 2: 255, 3: 255},
        4: 1000,
        5: True,
        6: False,
    }
    wait = {0: 1, 1: 6, 11: {0: 1, 1: 2}}
    snapshot = {0: 1, 1: "Game", 2: {0: [line], 1: []}, 5: wait, 6: settings}
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("presentation_snapshot", snapshot))
        worker.events.put(FrontendEvent("wait", wait))
        await pilot.pause(0.1)
        game_line = app.query_one(GameLine)
        assert game_line.regions[0].start == 0
        assert str(game_line.render()).startswith("开始")
        assert await pilot.hover(".game-line", offset=(1, 0))
        assert await pilot.click(".game-line", offset=(1, 0))
        assert ("activate", token) in worker.commands
        projection_revisions = [value[2] for kind, value in worker.commands if kind == "projection"]
        assert len(projection_revisions) >= 2
        assert all(
            current > previous
            for previous, current in zip(projection_revisions, projection_revisions[1:])
        )

        worker.commands.clear()
        assert await pilot.hover(".game-line", offset=(1, 0))
        assert game_line.hovered_region is None
        assert await pilot.click(".game-line", offset=(1, 0))
        assert not any(kind == "activate" for kind, _value in worker.commands)

        app._handle_worker_event(FrontendEvent("interaction_rejected", wait))
        assert game_line.regions[0].enabled
        assert app.query_one(GameViewport).interactions_enabled
        assert await pilot.click(".game-line", offset=(1, 0))
        assert ("activate", token) in worker.commands

        worker.commands.clear()
        app.active_wait = {0: 7, 1: 0}
        assert await pilot.click(".game-line", offset=(1, 0), button=3)
        assert ("skip_enter_waits", None) in worker.commands
        assert not any(kind == "activate" for kind, _value in worker.commands)

        worker.commands.clear()
        worker.events.put(
            FrontendEvent(
                "presentation_delta",
                {0: 1, 1: 2, 2: [variant(13, 1)]},
            )
        )
        await pilot.pause(0.1)
        assert not game_line.regions[0].enabled
        assert await pilot.hover(".game-line", offset=(1, 0))
        assert game_line.hovered_region is None
        assert await pilot.click(".game-line", offset=(1, 0))
        assert not any(kind == "activate" for kind, _value in worker.commands)


async def test_save_delete_button_requires_confirmation(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    token = {0: 4, 1: 2}
    style = {
        0: {0: 255, 1: 255, 2: 255, 3: 255},
        2: False,
        3: False,
        4: False,
        5: False,
        7: 12_000,
    }
    button = variant(
        1,
        [variant(0, "删除", style, None)],
        token,
        "Delete save01.sav",
        None,
        variant(0, 0),
        0,
        True,
    )
    line = {0: 1, 1: False, 2: True, 3: True, 4: 0, 5: [button]}
    wait = {0: 1, 1: 6, 11: {0: 1, 1: 2}}
    snapshot = {0: 1, 1: "Game", 2: {0: [line], 1: []}, 5: wait}

    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("presentation_snapshot", snapshot))
        worker.events.put(FrontendEvent("wait", wait))
        await pilot.pause(0.1)

        assert await pilot.click(".game-line", offset=(1, 0))
        await pilot.pause()
        assert (
            app.screen.query_one("#confirm-message", Static)
            .render()
            .plain.endswith("save01.sav 吗？")
        )
        assert not any(kind == "activate" for kind, _value in worker.commands)

        await pilot.click("#confirm-cancel")
        await pilot.pause()
        assert not any(kind == "activate" for kind, _value in worker.commands)

        assert await pilot.click(".game-line", offset=(1, 0))
        await pilot.pause()
        await pilot.click("#confirm-accept")
        await pilot.pause()
        assert ("activate", token) in worker.commands


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


async def test_snapshot_export_locks_gameplay_and_uses_a_timestamped_name(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)):
        app.active_wait = {0: 7, 1: 0, 11: {0: 1, 1: 2}}
        app.input_undo_token = {0: 1}
        app._update_prompt()
        assert app._snapshot_default_path(datetime(2026, 7, 22, 9, 8, 7)).name == (
            "runtime_20260722-090807.snapshot"
        )

        target = tmp_path / "runtime.snapshot"
        app._start_snapshot_export(target)

        prompt = app.query_one("#prompt", Input)
        viewport = app.query_one(GameViewport)
        assert app.snapshot_exporting
        assert prompt.disabled
        assert prompt.placeholder == "VM 快照导出中……"
        assert not viewport.interactions_enabled
        app.on_game_viewport_continue_requested(None)  # type: ignore[arg-type]
        app.on_game_viewport_skip_enter_requested(None)  # type: ignore[arg-type]
        app.action_input_undo()
        assert ("export_snapshot", (target, "normal")) in worker.commands
        assert not any(
            kind in ("submit_text", "skip_enter_waits", "input_undo")
            for kind, _value in worker.commands
        )

        app._handle_worker_event(FrontendEvent("snapshot_export_finished", True))

        assert not app.snapshot_exporting
        assert not prompt.disabled
        assert viewport.interactions_enabled


async def test_fatal_fault_dialog_exports_diagnosis_and_gates_recovery_actions(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app._log("DEBUG: before fault", LogLevel.DEBUG)
        failure = RuntimeFailure(code=3, message="place storage is unavailable")
        app._handle_worker_event(FrontendEvent("runtime_fault", failure))
        await pilot.pause()

        dialog = app.screen
        assert isinstance(dialog, FatalErrorDialog)
        assert "无法恢复" in str(dialog.query_one(".fatal-title", Static).render())
        assert "place storage is unavailable" in str(
            dialog.query_one("#fatal-error", Static).render()
        )
        assert "before fault" in app.fault_logs
        assert "place storage is unavailable" in app.fault_logs
        assert "DEBUG before fault" in app.fault_logs
        assert "DEBUG: before fault" not in app.fault_logs

        app._start_diagnosis_export(None)
        assert not app.diagnosis_exporting

        app._log("post-fault detail", LogLevel.DEBUG)
        target = tmp_path / "eraTW-diagnosis_20260726-140506.tar.zst"
        app._start_diagnosis_export(target)
        assert app.diagnosis_exporting
        assert "DEBUG post-fault detail" in app.fault_logs
        assert ("export_diagnosis", (target, app.fault_logs)) in worker.commands
        assert all(button.disabled for button in dialog.query(".fatal-buttons Button"))
        assert "正在导出" in str(dialog.query_one("#fatal-export-status", Static).render())

        app._handle_worker_event(FrontendEvent("diagnosis_export_finished", (True, str(target))))
        assert not app.diagnosis_exporting
        assert all(not button.disabled for button in dialog.query(".fatal-buttons Button"))
        assert "导出成功" in str(dialog.query_one("#fatal-export-status", Static).render())

        app.on_fatal_error_dialog_action(FatalErrorDialog.Action("recompile"))
        assert ("restart_recompile", None) in worker.commands


async def test_debug_mode_marks_manual_snapshot_exports(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)):
        app.debug_enabled = True
        target = tmp_path / "debug.snapshot"
        app._start_snapshot_export(target)
        assert ("export_snapshot", (target, "debug")) in worker.commands


async def test_gameplay_stays_locked_until_presentation_refresh_finishes(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)):
        app.active_wait = {0: 8, 1: 0, 11: {0: 1, 1: 3}}
        app.presentation.revision = 4

        app._begin_presentation_render()

        prompt = app.query_one("#prompt", Input)
        viewport = app.query_one(GameViewport)
        assert app.presentation_rendering
        assert prompt.disabled
        assert prompt.placeholder == "页面渲染中……"
        assert not viewport.interactions_enabled
        app.on_game_viewport_continue_requested(None)  # type: ignore[arg-type]
        assert not any(kind == "submit_text" for kind, _value in worker.commands)

        app._finish_presentation_render(4)

        assert not app.presentation_rendering
        assert not prompt.disabled
        assert viewport.interactions_enabled


async def test_presentation_background_reaches_existing_and_new_game_lines(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    line = DisplayLineModel(1, False, True, True, 0, (DisplaySegment("time stopped"),))
    async with app.run_test(size=(100, 30)) as pilot:
        app.presentation.lines = [line]
        app.presentation.background = "#01183c"
        worker.events.put(
            FrontendEvent(
                "presentation_snapshot",
                {
                    0: 1,
                    1: "Game",
                    2: {0: [], 1: []},
                    6: {2: {0: 1, 1: 24, 2: 60, 3: 255}, 3: {0: 0, 1: 0, 2: 0, 3: 255}},
                },
            )
        )
        await pilot.pause(0.1)
        viewport = app.query_one(GameViewport)
        assert str(viewport.styles.background) == "Color(1, 24, 60)"

        app.presentation.lines = [line]
        await viewport.set_lines([line])
        child = app.query_one(GameLine)
        viewport.set_presentation_background("#01183c")
        assert str(child.styles.background) == "Color(1, 24, 60)"
        viewport.set_presentation_background("#000000")
        assert str(child.styles.background) == "Color(0, 0, 0)"


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
