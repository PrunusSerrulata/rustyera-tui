from __future__ import annotations

from app_test_support import (
    AboutDialog,
    Button,
    DiagnosisProgress,
    ExportProgressDialog,
    FakeWorker,
    FatalErrorDialog,
    FrontendEvent,
    GameInformation,
    GameViewport,
    Input,
    LogLevel,
    LogMessage,
    Path,
    PathDialog,
    ProgressBar,
    RuntimeFailure,
    RustyEraTui,
    Static,
    Text,
    datetime,
)


async def test_help_menu_exports_diagnosis_and_shows_about_information(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app.presentation.title = "eraThe World"
        worker.events.put(FrontendEvent("phase", 4))
        await pilot.pause()
        await pilot.click("#menu-help")
        assert app.query_one("#help-menu").has_class("visible")
        assert [str(button.label) for button in app.query("#help-menu Button")] == [
            "导出诊断信息…",
            "关于…",
        ]

        await pilot.click("#help-export-diagnosis")
        assert isinstance(app.screen, PathDialog)
        assert app.screen.initial_value.parent == tmp_path
        assert app.screen.initial_value.name.startswith("eraThe World-diagnosis_")
        assert app.screen.initial_value.name.endswith(".tar.zst")
        await pilot.click("#path-cancel")

        await pilot.click("#menu-help")
        await pilot.click("#help-about")
        assert isinstance(app.screen, AboutDialog)
        contents = "\n".join(str(item.render()) for item in app.screen.query(Static))
        assert "作者：PrunusSerrulata" in contents
        assert "前端版本：0.7.0" in contents
        pinned_revision = (Path(__file__).parent.parent / "rustyera-core.rev").read_text().strip()
        assert f"core 版本：0.7.0 ({pinned_revision[:8]})" in contents
        assert "许可证：GPL-3.0-only" in contents
        assert "仅适用于 RustyEra 相关组件" in contents
        assert "https://github.com/PrunusSerrulata/rustyera-core" in contents
        assert "https://github.com/PrunusSerrulata/rustyera-tui" in contents
        title = app.screen.query_one(".dialog-title")
        assert title.styles.margin.bottom == 1
        note = app.screen.query_one("#about-license-note", Static)
        assert note.styles.padding.left == 8
        assert note.styles.color.hex6 == "#9CA3AF"
        assert note.styles.text_style.italic
        for widget_id, url in (
            ("#about-core-repository", "https://github.com/PrunusSerrulata/rustyera-core"),
            ("#about-tui-repository", "https://github.com/PrunusSerrulata/rustyera-tui"),
        ):
            content = app.screen.query_one(widget_id, Static).content
            assert isinstance(content, Text)
            assert any(span.style == f"link {url}" for span in content.spans)
        assert len(app.screen.query("#about-game-separator")) == 0


def test_diagnosis_project_title_uses_shared_fallback_order(tmp_path: Path) -> None:
    project = tmp_path / "folder-name"
    app = RustyEraTui(project, None)
    app.presentation.title = "Presentation title"
    app.game_information = GameInformation(title="GameBase title")

    assert app._diagnosis_project_title() == "GameBase title"

    app.game_information = GameInformation()
    assert app._diagnosis_project_title() == "Presentation title"

    app.presentation.title = "RustyEra"
    assert app._diagnosis_project_title() == "folder-name"

    packaged = RustyEraTui(None, None, tmp_path / "packed-game.reraproj")
    assert packaged._diagnosis_project_title() == "packed-game"

    unnamed = RustyEraTui(Path("/"), None)
    assert unnamed._diagnosis_project_title() == "project"


async def test_project_loaded_updates_diagnosis_path_identity(tmp_path: Path) -> None:
    initial_file = tmp_path / "old-game.reraproj"
    app = RustyEraTui(None, None, initial_file)
    app.worker = FakeWorker()  # type: ignore[assignment]

    async with app.run_test(size=(100, 30)):
        assert app._diagnosis_project_title() == "old-game"

        current_file = tmp_path / "current-game.reraproj"
        app._handle_worker_event(
            FrontendEvent("project_loaded", (current_file.parent, current_file))
        )
        assert app._diagnosis_project_title() == "current-game"

        directory = tmp_path / "directory-game"
        app._handle_worker_event(FrontendEvent("project_loaded", (directory, None)))
        assert app._diagnosis_project_title() == "directory-game"


async def test_manual_diagnosis_export_reuses_fatal_export_payload(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app.presentation.title = "eraThe World"
        app._log("manual diagnosis detail", LogLevel.WARNING)
        target = tmp_path / "manual.tar.zst"

        app._start_diagnosis_export(target)
        await pilot.pause()

        assert app.diagnosis_exporting
        assert app.query_one("#prompt", Input).disabled
        diagnosis_progress = app.query_one("#diagnosis-progress")
        assert diagnosis_progress.has_class("visible")
        assert diagnosis_progress.region.bottom <= app.query_one("#prompt-row").region.y
        assert app.query_one(GameViewport).region.bottom <= diagnosis_progress.region.y
        app._handle_worker_event(
            FrontendEvent("diagnosis_progress", DiagnosisProgress("vm_snapshot", 3, 4))
        )
        assert "75%" in str(app.query_one("#diagnosis-progress-label", Static).render())
        progress = app.query_one("#diagnosis-progress-bar", ProgressBar)
        assert progress.progress == 3
        assert progress.total == 4
        assert not app.query_one(GameViewport).interactions_enabled
        assert "manual diagnosis detail" in app.fault_logs
        assert (
            "export_diagnosis",
            (target, app.fault_logs, "eraThe World"),
        ) in worker.commands
        app._handle_worker_event(
            FrontendEvent("diagnosis_export_finished", (False, "archive failed"))
        )
        assert not app.diagnosis_exporting
        assert not diagnosis_progress.has_class("visible")


def test_log_export_default_path_uses_timestamp(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    now = datetime(2026, 7, 26, 14, 5, 6)

    assert app._log_default_path(now) == tmp_path / "log_20260726-140506.log"


async def test_operation_sequence_export_uses_file_menu_and_locks_gameplay(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("phase", 4))
        await pilot.pause()
        app.active_wait = {0: 7, 1: 0, 11: {0: 1, 1: 2}}
        app._update_prompt()
        assert app._input_replay_default_path(datetime(2026, 7, 26, 14, 5, 6)) == (
            tmp_path / "input-replay_20260726-140506.jsonl"
        )

        await pilot.click("#menu-file")
        await pilot.click("#file-export-input-replay")
        assert isinstance(app.screen, PathDialog)
        assert app.screen.initial_value.parent == tmp_path
        assert app.screen.initial_value.name.startswith("input-replay_")
        assert app.screen.initial_value.suffix == ".jsonl"
        target = tmp_path / "input-replay.jsonl"
        app.screen.query_one("#path-value", Input).value = str(target)
        await pilot.click("#path-accept")

        prompt = app.query_one("#prompt", Input)
        viewport = app.query_one(GameViewport)
        assert app.input_replay_exporting
        assert prompt.disabled
        assert prompt.placeholder == "操作序列导出中……"
        assert not viewport.interactions_enabled
        assert ("export_input_replay", target) in worker.commands

        app._handle_worker_event(FrontendEvent("input_replay_export_finished", True))

        assert not app.input_replay_exporting
        assert not prompt.disabled
        assert viewport.interactions_enabled


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
        app.on_game_viewport_skip_message_requested(None)  # type: ignore[arg-type]
        app.action_input_undo()
        assert ("export_snapshot", (target, "normal")) in worker.commands
        assert not any(
            kind in ("submit_text", "skip_message_waits", "input_undo")
            for kind, _value in worker.commands
        )

        app._handle_worker_event(FrontendEvent("snapshot_export_finished", True))

        assert not app.snapshot_exporting
        assert not prompt.disabled
        assert viewport.interactions_enabled


async def test_full_project_export_uses_a_cancellable_modal_and_locks_gameplay(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app.active_wait = {0: 7, 1: 0, 11: {0: 1, 1: 2}}
        app._update_prompt()
        target = tmp_path / "full.reraproj"

        app._start_project_file_export(target)
        await pilot.pause()

        assert isinstance(app.screen, ExportProgressDialog)
        assert app.project_file_exporting
        assert app.query_one("#prompt", Input).disabled
        assert not app.query_one(GameViewport).interactions_enabled
        assert ("export_project_file", target) in worker.commands

        await pilot.click("#export-progress-cancel")
        await pilot.pause()

        assert not app.project_file_exporting
        assert ("cancel_project_file_export", None) in worker.commands
        assert not app.query_one("#prompt", Input).disabled


async def test_background_cache_packaging_progress_does_not_lock_gameplay(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)):
        app._handle_worker_event(FrontendEvent("phase", 5))
        app.active_wait = {0: 7, 1: 0, 11: {0: 1, 1: 2}}
        app._update_prompt()

        app._handle_worker_event(FrontendEvent("project_progress", (9, 4, 12)))

        assert not app.project_file_exporting
        assert not app.query_one("#prompt", Input).disabled
        assert app.query_one(GameViewport).interactions_enabled


async def test_fatal_fault_dialog_exports_diagnosis_and_gates_recovery_actions(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app.presentation.title = "eraThe World"
        app._log("DEBUG: before fault", LogLevel.DEBUG)
        failure = RuntimeFailure(code=3, message="place storage is unavailable")
        app._handle_worker_event(
            FrontendEvent(
                "log",
                LogMessage(
                    LogLevel.ERROR,
                    "runtime fault [MissingPlaceStorage]: place storage is unavailable",
                    authoritative=True,
                ),
            )
        )
        app._handle_worker_event(FrontendEvent("runtime_fault", failure))
        await pilot.pause()

        dialog = app.screen
        assert isinstance(dialog, FatalErrorDialog)
        assert str(dialog.query_one(".fatal-title", Static).render()) == "游戏错误"
        assert "游戏遇到了无法恢复的错误：" in str(
            dialog.query_one(".fatal-description", Static).render()
        )
        fatal_error = dialog.query_one("#fatal-error", Static)
        assert "Runtime 故障 [VmFault]" in str(fatal_error.render())
        assert fatal_error.allow_select
        descriptions = list(dialog.query(".fatal-description"))
        assert "RustyEra 的开发者" in str(descriptions[-1].render())
        assert "VM 快照" in str(dialog.query_one("#fatal-export-status", Static).render())
        export_button = dialog.query_one("#fatal-export", Button)
        title_button = dialog.query_one("#fatal-title", Button)
        recompile_button = dialog.query_one("#fatal-recompile", Button)
        exit_button = dialog.query_one("#fatal-exit", Button)
        assert export_button.region.x < title_button.region.x
        assert export_button.region.right < title_button.region.x
        assert title_button.region.right <= recompile_button.region.x
        assert recompile_button.region.right <= exit_button.region.x
        assert "before fault" in app.fault_logs
        assert "place storage is unavailable" in app.fault_logs
        assert "DEBUG before fault" in app.fault_logs
        assert "DEBUG: before fault" not in app.fault_logs

        app._start_diagnosis_export(None)
        assert not app.diagnosis_exporting

        app._log("post-fault detail", LogLevel.DEBUG)
        target = tmp_path / "eraThe World-diagnosis_20260726-140506.tar.zst"
        app._start_diagnosis_export(target)
        assert app.diagnosis_exporting
        assert "DEBUG post-fault detail" in app.fault_logs
        assert (
            "export_diagnosis",
            (target, app.fault_logs, "eraThe World"),
        ) in worker.commands
        assert all(button.disabled for button in dialog.query(".fatal-buttons Button"))
        assert str(dialog.query_one("#fatal-export-status", Static).render()) == (
            "正在准备诊断信息…"
        )
        app._handle_worker_event(
            FrontendEvent("diagnosis_progress", DiagnosisProgress("project_transfer", 1, 4))
        )
        assert "25%" in str(dialog.query_one("#fatal-export-status", Static).render())
        fatal_progress = dialog.query_one("#fatal-export-progress", ProgressBar)
        assert fatal_progress.has_class("visible")
        assert fatal_progress.progress == 1
        assert fatal_progress.total == 4

        app._handle_worker_event(FrontendEvent("diagnosis_export_finished", (True, str(target))))
        assert not app.diagnosis_exporting
        assert all(not button.disabled for button in dialog.query(".fatal-buttons Button"))
        assert "导出成功" in str(dialog.query_one("#fatal-export-status", Static).render())
        assert not fatal_progress.has_class("visible")

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
