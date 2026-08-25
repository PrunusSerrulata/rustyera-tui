from __future__ import annotations

from app_test_support import (
    Button,
    DataTable,
    FakeWorker,
    FrontendEvent,
    Input,
    Path,
    PathDialog,
    RustyEraTui,
    Static,
    variant,
)


async def test_menu_hover_click_and_debug_gating(tmp_path: Path) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        controlled_items = (
            *app.GAME_FILE_ITEMS,
            *app.GAME_DEBUG_ITEMS,
            "help-export-diagnosis",
        )
        assert all(app.query_one(f"#{item_id}", Button).disabled for item_id in controlled_items)
        assert not app.query_one("#file-exit", Button).disabled
        assert not app.query_one("#debug-logs", Button).disabled
        file_menu = app.query_one("#file-menu")
        assert [
            child.id if isinstance(child, Button) else "separator" for child in file_menu.children
        ] == [
            "file-export-project",
            "file-restart",
            "file-title",
            "file-reload-all",
            "file-reload-folder",
            "file-reload-file",
            "separator",
            "file-export-input-replay",
            "file-export-snapshot",
            "file-restore-snapshot",
            "file-project-settings",
            "separator",
            "file-preferences",
            "separator",
            "file-exit",
        ]
        assert [str(button.label) for button in file_menu.query(Button)] == [
            "导出全量项目文件…",
            "重新开始",
            "返回标题",
            "重新加载全部脚本",
            "重新加载文件夹…",
            "重新加载单个脚本…",
            "导出操作序列…",
            "导出 VM 快照…",
            "恢复 VM 快照…",
            "项目设置…",
            "偏好设置…",
            "退出",
        ]
        debug_menu = app.query_one("#debug-menu")
        assert [
            child.id if isinstance(child, Button) else "separator" for child in debug_menu.children
        ] == [
            "debug-toggle",
            "separator",
            "debug-console",
            "debug-variables",
            "debug-stack",
            "debug-step-toggle",
            "separator",
            "debug-logs",
        ]
        assert [str(button.label) for button in debug_menu.query(Button)] == [
            "启用调试",
            "控制台…",
            "变量查看器…",
            "Fibers / 调用栈…",
            "开启单步运行",
            "日志…",
        ]

        worker.events.put(FrontendEvent("phase", 4))
        await pilot.pause(0.1)
        assert all(
            not app.query_one(f"#{item_id}", Button).disabled
            for item_id in (*app.GAME_FILE_ITEMS, "debug-toggle", "help-export-diagnosis")
        )
        await pilot.click("#menu-file")
        assert app.query_one("#file-menu").has_class("visible")
        await pilot.click("#file-reload-all")
        assert ("reload_all", None) in worker.commands

        await pilot.click("#menu-file")
        await pilot.click("#file-reload-folder")
        assert isinstance(app.screen, PathDialog)
        assert str(app.screen.query_one(".dialog-title").render()) == "重新加载文件夹"
        app.screen.query_one("#path-value", Input).value = str(tmp_path)
        await pilot.click("#path-accept")
        assert ("reload_folder", tmp_path) in worker.commands

        await pilot.click("#menu-file")
        await pilot.click("#file-reload-file")
        assert isinstance(app.screen, PathDialog)
        assert str(app.screen.query_one(".dialog-title").render()) == "重新加载单个脚本"
        app.screen.query_one("#path-value", Input).value = str(source)
        await pilot.click("#path-accept")
        assert ("reload_file", source) in worker.commands

        app.presentation.title = "测试项目"
        await pilot.click("#menu-file")
        await pilot.click("#file-export-project")
        assert isinstance(app.screen, PathDialog)
        assert app.screen.initial_value == tmp_path / "测试项目.reraproj"
        await pilot.click("#path-cancel")

        await pilot.click("#menu-debug")
        assert app.query_one("#debug-console").disabled
        await pilot.click("#debug-toggle")
        assert ("debug_enable", None) in worker.commands

        worker.events.put(FrontendEvent("phase", 8))
        await pilot.pause(0.1)
        assert all(app.query_one(f"#{item_id}", Button).disabled for item_id in controlled_items)

        worker.events.put(FrontendEvent("phase", 5))
        worker.events.put(FrontendEvent("debug_enabled", True))
        await pilot.pause(0.1)
        assert str(app.query_one("#debug-toggle", Button).label) == "禁用调试"
        assert all(
            not app.query_one(f"#{item_id}", Button).disabled for item_id in controlled_items
        )

        assert not app.query("#menu-line-number")
        assert str(app.query_one("#prompt-label").render()) == "> "
        separator = app.query_one("#separator-line")
        prompt_row = app.query_one("#prompt-row")
        assert separator.size.width == app.size.width
        assert separator.region.bottom == prompt_row.region.y
        assert app.query_one("#file-restart").styles.content_align[0] == "left"
        assert app.query_one("#menu-file").styles.content_align[0] == "left"


async def test_closed_debug_dialogs_release_app_references(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        worker.events.put(FrontendEvent("phase", 5))
        worker.events.put(FrontendEvent("debug_enabled", True))
        await pilot.pause(0.1)

        for menu_item, attribute, owner in (
            ("debug-console", "console_dialog", "console"),
            ("debug-variables", "variable_dialog", "variables"),
            ("debug-stack", "stack_dialog", "stack"),
        ):
            await pilot.click("#menu-debug")
            await pilot.click(f"#{menu_item}")
            assert getattr(app, attribute) is app.screen
            await pilot.click("#dialog-close")
            await pilot.pause()
            assert getattr(app, attribute) is None
            assert ("debug_surface_closed", owner) in worker.commands


async def test_session_reset_dismisses_debug_dialog_without_stale_runtime_command(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_worker_event(FrontendEvent("phase", 5))
        app._set_debug_enabled(True)
        app._debug_action("debug-console")
        await pilot.pause()
        worker.commands.clear()

        app._handle_worker_event(FrontendEvent("session_reset"))
        await pilot.pause()

        assert app.console_dialog is None
        assert not any(kind == "debug_surface_closed" for kind, _value in worker.commands)


async def test_single_step_prompt_shows_source_and_f10_advances(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_worker_event(FrontendEvent("phase", 5))
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
        app._handle_worker_event(FrontendEvent("phase", 5))
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
        app._handle_worker_event(FrontendEvent("phase", 5))
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
        app._handle_worker_event(FrontendEvent("phase", 5))
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
