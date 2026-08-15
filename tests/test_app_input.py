from __future__ import annotations

from app_test_support import (
    AboutDialog,
    Any,
    CORE_VERSION,
    FakeWorker,
    FrontendEvent,
    GameLine,
    GameViewport,
    Input,
    Path,
    PresentationBatch,
    RustyEraTui,
    Static,
    events,
    variant,
)


async def test_prompt_submits_through_worker(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt")
        app.active_wait = {0: 4, 1: 2, 11: {0: 1, 1: 4}}
        prompt.disabled = False
        prompt.focus()
        await pilot.press("1", "2", "enter")
        assert ("submit_text", "12") in worker.commands
        await pilot.press("3", "enter")
        assert worker.commands.count(("submit_text", "12")) == 1
        assert not any(command == ("submit_text", "3") for command in worker.commands)
        app._handle_worker_event(FrontendEvent("interaction_rejected", app.active_wait))
        await pilot.pause()
        await pilot.press("4", "enter")
        assert ("submit_text", "4") in worker.commands

        app._handle_worker_event(FrontendEvent("interaction_rejected", app.active_wait))
        await pilot.pause(0.1)
        app.input_undo_token = {0: 2, 1: 9}
        app.action_input_undo()
        app.action_input_undo()
        assert worker.commands.count(("input_undo", {0: 2, 1: 9})) == 1


async def test_viewport_and_keyboard_submit_message_waits_once(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:

        def input_commands() -> list[tuple[str, Any]]:
            return [command for command in worker.commands if command[0] != "projection"]

        async def set_wait(wait: dict[int, Any]) -> None:
            worker.events.put(FrontendEvent("wait", wait))
            await pilot.pause(0.1)
            worker.commands.clear()

        await set_wait({0: 7, 1: 0})
        await pilot.click("#game-viewport", offset=(10, 5))
        assert input_commands() == [("submit_text", "")]

        await set_wait({0: 8, 1: 0})
        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        assert input_commands() == [("skip_message_waits", None)]

        await set_wait({0: 9, 1: 1})
        await pilot.click("#game-viewport", offset=(10, 5))
        assert input_commands() == [("submit_text", "")]

        await set_wait({0: 10, 1: 1})
        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        assert input_commands() == [("skip_message_waits", None)]

        await set_wait({0: 13, 1: 1, 4: True, 11: {0: 2, 1: 13}})
        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        assert input_commands() == []
        assert app._activated_wait is None
        await pilot.click("#game-viewport", offset=(10, 5))
        assert input_commands() == [("submit_text", "")]

        await set_wait({0: 14, 1: 2})
        await pilot.click("#game-viewport", offset=(10, 5))
        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        assert input_commands() == []

    keyboard_app = RustyEraTui(tmp_path, None)
    keyboard_worker = FakeWorker()
    keyboard_app.worker = keyboard_worker  # type: ignore[assignment]
    async with keyboard_app.run_test(size=(100, 30)) as pilot:
        keyboard_worker.events.put(FrontendEvent("wait", {0: 15, 1: 1, 11: {0: 2, 1: 15}}))
        await pilot.pause(0.1)
        keyboard_worker.commands.clear()
        prompt = keyboard_app.query_one("#prompt", Input)
        prompt.focus()
        assert not prompt.disabled
        assert keyboard_app.focused is prompt

        keyboard_app.on_key(events.Key("a", "a"))
        assert [command for command in keyboard_worker.commands if command[0] != "projection"] == [
            ("submit_text", "a")
        ]
        await pilot.click("#game-viewport", offset=(10, 5))
        await pilot.click("#game-viewport", offset=(10, 5), button=3)
        keyboard_app.query_one("#prompt", Input).focus()
        await pilot.press("enter")

        assert [command for command in keyboard_worker.commands if command[0] != "projection"] == [
            ("submit_text", "a")
        ]


async def test_game_any_key_does_not_cross_an_application_dialog(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app.active_wait = {0: 7, 1: 1, 11: {0: 1, 1: 7}}
        app.push_screen(AboutDialog("test", CORE_VERSION))
        await pilot.pause()

        await pilot.press("space")

        assert [command for command in worker.commands if command[0] != "projection"] == []


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
    explicit_hover = {
        0: {0: 204, 1: 0, 2: 204, 3: 255},
        2: False,
        3: False,
        4: False,
        5: False,
        7: 12_000,
    }
    explicit_button = variant(
        1,
        [variant(0, "专用", style, None)],
        {0: 4, 1: 10},
        "专用颜色",
        explicit_hover,
        variant(0, 0),
        0,
        True,
    )
    line = {0: 1, 1: False, 2: True, 3: True, 4: 0, 5: [button, explicit_button]}
    settings = {
        0: 100_000,
        1: 1_000,
        2: {0: 0, 1: 0, 2: 0, 3: 255},
        3: {0: 51, 1: 102, 2: 153, 3: 255},
        4: 1000,
        5: True,
        6: False,
    }
    wait = {0: 1, 1: 6, 11: {0: 1, 1: 2}}
    snapshot = {0: 1, 1: "Game", 2: {0: [line], 1: []}, 5: wait, 6: settings}
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(snapshot, None, wait, True),
            )
        )
        await pilot.pause(0.1)
        game_line = app.query_one(GameLine)
        assert game_line.regions[0].start == 0
        assert str(game_line.render()).startswith("开始")
        assert await pilot.hover(".game-line", offset=(1, 0))
        hovered_style = game_line.render().get_style_at_offset(0)
        assert hovered_style.foreground is not None
        assert hovered_style.foreground.rgb == (51, 102, 153)
        assert not hovered_style.reverse
        assert not hovered_style.underline
        assert await pilot.hover(".game-line", offset=(5, 0))
        explicit_style = game_line.render().get_style_at_offset(2)
        assert explicit_style.foreground is not None
        assert explicit_style.foreground.rgb == (204, 0, 204)
        assert not explicit_style.reverse
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
        await pilot.pause(0.1)
        assert game_line.regions[0].enabled
        assert app.query_one(GameViewport).interactions_enabled
        assert await pilot.click(".game-line", offset=(1, 0))
        assert ("activate", token) in worker.commands

        worker.commands.clear()
        app.active_wait = {0: 7, 1: 0}
        assert await pilot.click(".game-line", offset=(1, 0), button=3)
        assert ("skip_message_waits", None) in worker.commands
        assert not any(kind == "activate" for kind, _value in worker.commands)

        worker.commands.clear()
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(
                    None,
                    {0: 1, 1: 2, 2: [variant(13, 1)]},
                    wait,
                    True,
                ),
            )
        )
        await pilot.pause(0.1)
        assert not game_line.regions[0].enabled
        assert await pilot.hover(".game-line", offset=(1, 0))
        assert game_line.hovered_region is None
        assert await pilot.click(".game-line", offset=(1, 0))
        assert not any(kind == "activate" for kind, _value in worker.commands)

        current_token = {0: 4, 1: 11}
        current_button = variant(
            1,
            [variant(0, "当前地图", style, None)],
            current_token,
            "当前地图",
            None,
            variant(0, 1),
            1,
            True,
        )
        current_line = {
            0: 1,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [current_button],
        }
        next_wait = {0: 2, 1: 6, 11: {0: 1, 1: 3}}
        worker.commands.clear()
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(
                    None,
                    {0: 2, 1: 3, 2: [variant(7, 1, current_line)]},
                    next_wait,
                    True,
                ),
            )
        )
        await pilot.pause(0.1)
        assert game_line.regions[0].enabled
        assert await pilot.click(".game-line", offset=(1, 0))
        assert ("activate", current_token) in worker.commands


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
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(snapshot, None, wait, True),
            )
        )
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
