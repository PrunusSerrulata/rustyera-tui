from __future__ import annotations

from app_test_support import (
    Button,
    CORE_VERSION,
    ConfirmDialog,
    FakeWorker,
    FrontendEvent,
    GameInformation,
    Input,
    Path,
    Rule,
    RustyEraTui,
    Static,
)


async def test_mount_does_not_restart_a_prestarted_runtime_worker(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.start()
    app = RustyEraTui(tmp_path, None, worker=worker)  # type: ignore[arg-type]

    async with app.run_test(size=(100, 30)):
        assert worker.ident == 1
        assert worker.started
        assert worker.start_calls == 1


async def test_restart_and_return_to_title_require_confirmation(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]

    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("phase", 4))
        await pilot.pause()

        await pilot.click("#menu-file")
        await pilot.click("#file-restart")
        worker.events.put(FrontendEvent("phase", 5))
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ConfirmDialog)
        assert ("restart", None) not in worker.commands
        assert app.focused is app.query_one("#menu-file", Button)

        for item_id, title, command in (
            ("file-restart", "重新开始游戏", "restart"),
            ("file-title", "返回标题", "return_title"),
        ):
            await pilot.click("#menu-file")
            await pilot.click(f"#{item_id}")
            assert isinstance(app.screen, ConfirmDialog)
            assert str(app.screen.query_one(".dialog-title").render()) == title
            assert "可能会丢失尚未保存的游戏进度" in str(
                app.screen.query_one("#confirm-message", Static).render()
            )
            assert (command, None) not in worker.commands

            await pilot.click("#confirm-cancel")
            await pilot.pause()
            assert (command, None) not in worker.commands
            assert app.focused is app.query_one("#menu-file", Button)

            await pilot.click("#menu-file")
            await pilot.click(f"#{item_id}")
            await pilot.click("#confirm-accept")
            await pilot.pause()
            assert (command, None) in worker.commands
            assert app.focused is app.query_one("#menu-file", Button)


async def test_about_information_shows_only_defined_game_metadata(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    app.game_information = GameInformation.from_wire({0: "Demo", 1: "   ", 2: "1.001", 4: "Notes"})

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.click("#menu-help")
        await pilot.click("#help-about")

        contents = "\n".join(str(item.render()) for item in app.screen.query(Static))
        separator = app.screen.query_one("#about-game-separator", Rule)
        assert separator.styles.margin.top == 0
        assert separator.styles.margin.bottom == 0
        assert "游戏名称：Demo" in contents
        assert "游戏版本：1.001" in contents
        assert "备注：Notes" in contents
        assert "游戏作者：" not in contents
        assert "游戏开发时间：" not in contents


def test_project_file_default_path_sanitizes_the_project_title(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    app.presentation.title = 'bad<>:"/\\|?* name. '

    assert app._project_file_default_path() == tmp_path / "bad_________ name.reraproj"


def test_displayed_core_revision_matches_the_build_pin() -> None:
    pinned_revision = (Path(__file__).parent.parent / "rustyera-core.rev").read_text().strip()

    assert CORE_VERSION.endswith(f"({pinned_revision[:8]})")


async def test_project_progress_shows_real_completed_work(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt", Input)
        assert not app.query("#project-progress")

        worker.events.put(FrontendEvent("status", "正在扫描 /game…"))
        await pilot.pause(0.1)
        assert prompt.disabled
        assert prompt.placeholder == "正在扫描 /game…"

        worker.events.put(FrontendEvent("project_progress", (0, 37, 80)))
        await pilot.pause(0.1)
        assert prompt.placeholder == "正在读取项目文件：37/80 [█████████░░░░░░░░░░░] 46%"

        worker.events.put(FrontendEvent("project_progress", (5, 64, 100)))
        await pilot.pause(0.1)
        assert prompt.placeholder == "正在编译脚本函数：64/100 [████████████░░░░░░░░] 64%"

        worker.events.put(FrontendEvent("phase", 5))
        await pilot.pause(0.1)
        assert prompt.placeholder == "Runtime 正在运行…"
