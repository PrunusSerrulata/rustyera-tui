from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

from rustyera_tui.app import RustyEraTui
from rustyera_tui.runtime import FrontendEvent, RuntimeFailure
from rustyera_tui.widgets import GameLine
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

        worker.commands.clear()
        app.active_wait = {0: 8, 1: 2}
        await pilot.click("#game-viewport", offset=(10, 5))
        assert ("submit_text", "") not in worker.commands


async def test_log_dialog_uses_selectable_read_only_text(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    app.logs = ["first line", "second line"]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.click("#menu-debug")
        await pilot.click("#debug-logs")
        view = app.screen.query_one("#log-view", TextArea)
        assert view.read_only
        view.select_all()
        assert view.selected_text == "first line\nsecond line"


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
    snapshot = {0: 1, 1: "Game", 2: {0: [line], 1: []}, 6: settings}
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("presentation_snapshot", snapshot))
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
