from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

from rustyera_tui.app import RustyEraTui
from rustyera_tui.runtime import FrontendEvent
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
        assert await pilot.hover(".game-line", offset=(8, 0))
        assert await pilot.click(".game-line", offset=(8, 0))
        assert ("activate", token) in worker.commands
