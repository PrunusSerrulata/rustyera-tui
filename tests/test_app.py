from __future__ import annotations

import queue
from datetime import datetime
from pathlib import Path
from typing import Any

from rustyera_tui.app import RustyEraTui
from rustyera_tui.presentation import DisplayLineModel, DisplaySegment
from rustyera_tui.runtime import FrontendEvent, RuntimeFailure
from rustyera_tui.widgets import GameLine, GameViewport
from rustyera_tui.wire import variant
from textual.widgets import Input, TextArea


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
        assert viewport.is_vertical_scroll_end

        viewport.scroll_home(animate=False, x_axis=False)
        await pilot.pause()
        assert not viewport.is_vertical_scroll_end

        await viewport.set_lines([*lines, DisplayLineModel(40, False, True, True, 0, ())])
        await pilot.pause()
        assert viewport.is_vertical_scroll_end


async def test_log_dialog_uses_selectable_read_only_text(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    app.logs = [f"line {index}" for index in range(100)]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.click("#menu-debug")
        await pilot.click("#debug-logs")
        view = app.screen.query_one("#log-view", TextArea)
        assert view.read_only
        await pilot.pause()
        assert view.scroll_y > 0
        assert view.is_vertical_scroll_end
        view.select_all()
        assert view.selected_text == "\n".join(app.logs)


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
        assert any("Runtime phase -> WaitingInput" in log for log in app.logs)
        assert any("调试暂停：StepCompleted" in log for log in app.logs)


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
        assert ("export_snapshot", target) in worker.commands
        assert not any(
            kind in ("submit_text", "skip_enter_waits", "input_undo")
            for kind, _value in worker.commands
        )

        app._handle_worker_event(FrontendEvent("snapshot_export_finished", True))

        assert not app.snapshot_exporting
        assert not prompt.disabled
        assert viewport.interactions_enabled


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
