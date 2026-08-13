from __future__ import annotations

from app_test_support import (
    Any,
    Button,
    DisplayLineModel,
    DisplaySegment,
    FakeWorker,
    FrontendEvent,
    GameLine,
    GameViewport,
    Input,
    LogLevel,
    LogMessage,
    Path,
    PresentationBatch,
    RichLog,
    RuntimeFailure,
    RustyEraTui,
    Select,
    pytest,
    variant,
)


async def test_gameplay_output_commits_once_at_the_next_wait_without_a_tail_flash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]

    def line(line_id: int, text: str, *, line_end: bool = True) -> dict[int, Any]:
        return {
            0: line_id,
            1: False,
            2: True,
            3: line_end,
            4: 0,
            5: [variant(0, text, None, None)],
        }

    first_wait = {0: 1, 1: 0, 11: {0: 1, 1: 1}}
    next_wait = {0: 2, 1: 0, 11: {0: 1, 1: 2}}
    snapshot = {
        0: 1,
        1: "Game",
        2: {0: [line(1, "before")], 1: []},
        5: first_wait,
    }
    async with app.run_test(size=(100, 20)) as pilot:
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(snapshot, None, first_wait, True),
            )
        )
        await pilot.pause(0.1)
        viewport = app.query_one(GameViewport)
        assert [model.segments[0].text for model in viewport.models] == ["before"]

        set_lines_calls = 0
        original_set_lines = viewport.set_lines

        async def counted_set_lines(*args: Any, **kwargs: Any) -> bool:
            nonlocal set_lines_calls
            set_lines_calls += 1
            return await original_set_lines(*args, **kwargs)

        monkeypatch.setattr(viewport, "set_lines", counted_set_lines)
        provisional = {
            0: 1,
            1: 2,
            2: [variant(6), variant(0, line(2, "x" * 240, line_end=False))],
        }
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(None, provisional, None, False),
            )
        )
        await pilot.pause(0.1)

        assert app.presentation.revision == 2
        assert len(app.presentation.lines) == 2
        assert [model.segments[0].text for model in viewport.models] == ["before"]
        assert set_lines_calls == 0
        assert not viewport.show_horizontal_scrollbar
        assert app.presentation_rendering
        assert app.query_one("#prompt", Input).disabled

        final_lines = [line(line_id, f"line {line_id}") for line_id in range(2, 12)]
        final = {
            0: 2,
            1: 3,
            2: [
                variant(1, 1),
                *(variant(0, value) for value in final_lines),
                variant(6, next_wait),
            ],
        }
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(None, final, next_wait, True),
            )
        )
        await pilot.pause(0.1)

        assert set_lines_calls == 1
        assert [model.segments[0].text for model in viewport.models] == [
            "before",
            *(f"line {line_id}" for line_id in range(2, 12)),
        ]
        assert not viewport.show_horizontal_scrollbar
        assert not app.presentation_rendering
        assert not app.query_one("#prompt", Input).disabled

        terminal = {
            0: 3,
            1: 4,
            2: [variant(6), variant(0, line(12, "terminal output"))],
        }
        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(None, terminal, None, False),
            )
        )
        await pilot.pause(0.1)
        assert set_lines_calls == 1
        assert viewport.models[-1].segments[0].text == "line 11"

        worker.events.put(
            FrontendEvent(
                "presentation_batch",
                PresentationBatch(None, None, None, True),
            )
        )
        await pilot.pause(0.1)
        assert set_lines_calls == 2
        assert viewport.models[-1].segments[0].text == "terminal output"


async def test_log_dialog_filters_entries_at_the_selected_threshold(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    app._log("debug message", LogLevel.DEBUG)
    app._log("info message", LogLevel.INFO)
    app._log("warned event", LogLevel.WARNING)
    app._log("failed event", LogLevel.ERROR)
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
        copy = app.screen.query_one("#log-copy", Button)
        export = app.screen.query_one("#log-export", Button)
        clear = app.screen.query_one("#log-clear", Button)
        close = app.screen.query_one("#dialog-close", Button)
        assert level.region.x < clear.region.x
        assert clear.region.y == close.region.y
        assert level.region.y == close.region.y
        assert copy.region.right <= export.region.x
        assert export.region.right <= clear.region.x
        assert clear.region.right < close.region.x
        assert view.allow_select

        level.value = "warning"
        await pilot.pause()
        await pilot.click("#log-copy")
        assert "warned event" in app.clipboard
        assert "failed event" in app.clipboard
        assert "info message" not in app.clipboard

        await pilot.click("#log-export")
        await pilot.pause()
        path_input = app.screen.query_one("#path-value", Input)
        assert Path(path_input.value).name.startswith("log_")
        assert Path(path_input.value).suffix == ".log"
        target = tmp_path / "filtered.log"
        path_input.value = str(target)
        await pilot.click("#path-accept")
        await pilot.pause()
        exported = target.read_text(encoding="utf-8")
        assert "warned event" in exported
        assert "failed event" in exported
        assert "info message" not in exported

        await pilot.click("#log-clear")
        assert app.logs == []
        assert len(view.lines) == 0


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


async def test_runtime_events_only_log_backend_authoritative_entries(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("phase", 5))
        worker.events.put(FrontendEvent("debug_stopped", {1: [2, []]}))
        worker.events.put(
            FrontendEvent(
                "log",
                LogMessage(LogLevel.DEBUG, "debug stopped: StepCompleted", authoritative=True),
            )
        )
        await pilot.pause(0.1)
        pause_log = next(log for log in app.logs if "debug stopped: StepCompleted" in log)
        assert pause_log.level is LogLevel.DEBUG
        assert all("Runtime phase ->" not in log.message for log in app.logs)
        assert len(app.logs) == 1


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
                "presentation_batch",
                PresentationBatch(
                    {
                        0: 1,
                        1: "Game",
                        2: {0: [], 1: []},
                        6: {
                            2: {0: 1, 1: 24, 2: 60, 3: 255},
                            3: {0: 0, 1: 0, 2: 0, 3: 255},
                        },
                    },
                    None,
                    None,
                    True,
                ),
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
