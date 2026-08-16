from __future__ import annotations

from app_test_support import (
    Button,
    Checkbox,
    ColorField,
    ColorPickerDialog,
    ConfigurationChange,
    ConfigurationSnapshot,
    FIELDS,
    FakeWorker,
    FrontendEvent,
    GameViewport,
    Input,
    PAGES,
    Path,
    PreferencesDialog,
    ProjectSettingsDialog,
    RustyEraTui,
    Select,
    Static,
    TabbedContent,
    configuration_entry,
    configuration_value,
)
from rustyera_tui.client_preferences import LoadedPreferences, PreferenceValues


def test_project_settings_schema_contains_exactly_39_visible_fields() -> None:
    assert len(FIELDS) == 39


def test_tui_adds_only_the_two_requested_text_settings() -> None:
    assert "ReplaceFullWidthSpaces" in FIELDS
    assert "CharacterWidthMode" in FIELDS
    assert "AudioVolume" not in FIELDS


async def test_client_preferences_are_separate_and_saved_from_ctrl_comma(tmp_path: Path) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    app.global_preferences = LoadedPreferences(
        tmp_path / "global" / "preferences-v1.json", PreferenceValues({})
    )
    app.project_preferences = LoadedPreferences(
        tmp_path / ".rustyera" / "preferences-v1.json", PreferenceValues({})
    )
    snapshot = ConfigurationSnapshot.from_wire(
        {0: 1, 1: b"digest", 2: [configuration_entry("UseMouse", "YES", 0)], 3: False}
    )

    async with app.run_test(size=(100, 32)) as pilot:
        worker.events.put(FrontendEvent("configuration", (snapshot, False)))
        await pilot.pause(0.1)
        await pilot.press("ctrl+comma")
        assert isinstance(app.screen, PreferencesDialog)
        override = app.screen.query_one("#preference-override-usemouse", Checkbox)
        override.value = True
        app.screen.query_one("#preference-usemouse", Checkbox).value = False
        await pilot.click("#preferences-apply")
        await pilot.pause()

        assert (
            "save_client_preferences",
            ("global", PreferenceValues({"UseMouse": "NO"})),
        ) in worker.commands


async def test_preferences_use_native_compact_controls_and_fit_the_terminal(
    tmp_path: Path,
) -> None:
    entries = [
        configuration_entry(code, *configuration_value(field)) for code, field in FIELDS.items()
    ]
    snapshot = ConfigurationSnapshot.from_wire({0: 1, 1: b"digest", 2: entries, 3: False})
    app = RustyEraTui(tmp_path, None)
    app.worker = FakeWorker()  # type: ignore[assignment]

    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(ProjectSettingsDialog(snapshot, False))
        await pilot.pause()
        dialog = app.screen.query_one(".preferences-dialog")
        assert (dialog.size.width, dialog.size.height) == (104, 26)

        tabs = app.screen.query_one("#preferences-tabs", TabbedContent)
        for page_spec in PAGES:
            tabs.active = f"preferences-{page_spec.id}"
            await pilot.pause()
            for code in page_spec.codes:
                field = FIELDS[code]
                selector = f"#preference-{code.lower().replace('_', '-')}"
                control = app.screen.query_one(selector)
                assert control.size.height == 1
                if field.kind == "boolean":
                    assert type(control) is Checkbox
                    assert control.compact
                    assert control.BUTTON_INNER == "X"
                    assert control.label.plain == field.label
                    assert not control.parent.query(".preference-field-label")
                elif field.kind == "integer":
                    assert isinstance(control, Input)
                    assert control.type == "integer"
                    assert control.compact
                elif isinstance(control, (Input, Select)):
                    assert control.compact
                elif isinstance(control, ColorField):
                    assert control.query_one(Button).size.height == 1

        for button in app.screen.query(".preferences-dialog Button"):
            assert button.styles.content_align == ("center", "middle")
        for button in app.screen.query(".preferences-actions Button"):
            assert button.size.height == 1

        tabs.active = "preferences-interface"
        await pilot.pause()
        use_mouse = app.screen.query_one("#preference-usemouse", Checkbox)
        original = use_mouse.value
        use_mouse.focus()
        await pilot.press("space")
        assert use_mouse.value is not original

        await pilot.resize_terminal(90, 28)
        await pilot.pause()
        assert dialog.size.width <= 87
        assert dialog.size.height <= 27
        page = app.screen.query_one(".preferences-page-scroll")
        assert page.max_scroll_y > 0
        actions = app.screen.query_one(".preferences-actions")
        assert actions.region.bottom <= app.size.height


async def test_preferences_dialog_edits_runtime_configuration_and_honors_fixed_values(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    entries = [
        configuration_entry("UseMouse", "YES", 0, effective="NO"),
        configuration_entry("MaxLog", "1000", 1),
        configuration_entry("ForeColor", "192,192,192", 4, fixed=True),
        configuration_entry("BackColor", "0,0,0", 4),
        configuration_entry("AutoSave", "YES", 0, application=1),
        configuration_entry("SystemSaveInBinary", "NO", 0, application=1),
        configuration_entry("ZipSaveData", "NO", 0, application=1),
    ]
    snapshot = ConfigurationSnapshot.from_wire({0: 9, 1: b"digest", 2: entries, 3: False})

    async with app.run_test(size=(120, 40)) as pilot:
        worker.events.put(FrontendEvent("phase", 4))
        worker.events.put(FrontendEvent("configuration", (snapshot, False)))
        await pilot.pause(0.1)
        assert app.query_one("#menu-bar").display
        assert not app.query_one(GameViewport).mouse_enabled
        assert not app.query_one("#file-project-settings", Button).disabled

        await pilot.click("#menu-file")
        await pilot.click("#file-project-settings")
        assert isinstance(app.screen, ProjectSettingsDialog)
        dialog = app.screen.query_one(".preferences-dialog")
        assert dialog.size.width < app.size.width
        assert dialog.size.height < app.size.height
        assert len(app.screen.query(TabbedContent)) == 1
        for code in ("usemouse", "autosave", "systemsaveinbinary", "zipsavedata"):
            assert isinstance(app.screen.query_one(f"#preference-{code}"), Checkbox)
        max_log = app.screen.query_one("#preference-maxlog", Input)
        assert max_log.type == "integer"
        max_log.value = ""
        await pilot.click("#preferences-apply")
        await pilot.pause()
        assert not any(command == "save_configuration" for command, _value in worker.commands)
        max_log.value = "499"
        await pilot.click("#preferences-apply")
        await pilot.pause()
        assert not any(command == "save_configuration" for command, _value in worker.commands)
        max_log.value = "1200"
        await pilot.click("#preferences-reset")
        assert max_log.value == "1000"
        fixed_color = app.screen.query_one("#preference-forecolor", ColorField)
        assert fixed_color.query_one(Button).disabled
        use_mouse = app.screen.query_one("#preference-usemouse", Checkbox)
        use_mouse.value = False
        await pilot.click("#preferences-apply")
        await pilot.pause()
        assert (
            "save_configuration",
            ([ConfigurationChange("UseMouse", "NO")], False),
        ) in worker.commands
        assert isinstance(app.screen, ProjectSettingsDialog)

        updated_entries = [
            configuration_entry("UseMouse", "NO", 0),
            *entries[1:],
        ]
        updated = ConfigurationSnapshot.from_wire({0: 9, 1: b"next", 2: updated_entries, 3: True})
        worker.events.put(FrontendEvent("configuration", (updated, False)))
        await pilot.pause(0.1)
        assert isinstance(app.screen, ProjectSettingsDialog)
        assert "需重启" in str(app.screen.query_one("#preferences-status", Static).render())

        tabs = app.screen.query_one("#preferences-tabs", TabbedContent)
        tabs.active = "preferences-save"
        await pilot.pause()
        zip_save = app.screen.query_one("#preference-zipsavedata", Checkbox)
        assert zip_save.disabled
        app.screen.query_one("#preference-systemsaveinbinary", Checkbox).value = True
        await pilot.pause()
        assert not zip_save.disabled

        tabs.active = "preferences-interface"
        await pilot.pause()
        color = app.screen.query_one("#preference-backcolor", ColorField)
        await pilot.click(f"#{color.id}-choose")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ColorPickerDialog)
        assert len(app.screen.query("#color-grid Button")) == 216
        for input_control in app.screen.query(".color-picker-dialog Input"):
            assert input_control.compact
            assert input_control.size.height == 1
        for component in ("red", "green", "blue"):
            assert app.screen.query_one(f"#color-{component}", Input).type == "integer"
        for button in app.screen.query(".color-picker-dialog Button"):
            assert button.size.height == 1
            assert button.styles.content_align == ("center", "middle")
        assert app.screen.query_one(".color-picker-dialog .dialog-buttons").size.height == 1
        app.screen.query_one("#color-hex", Input).value = "#GGGGGG"
        await pilot.click("#color-confirm")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ColorPickerDialog)
        app.screen.query_one("#color-hex", Input).value = "#336699"
        await pilot.pause(0.1)
        await pilot.click("#color-confirm")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ProjectSettingsDialog)
        assert color.value == "51,102,153"

        await pilot.click("#preferences-cancel")
        await pilot.pause()
        app.action_project_settings()
        await pilot.pause()
        assert isinstance(app.screen, ProjectSettingsDialog)
        await pilot.click("#preferences-apply-restart")
        await pilot.pause()
        assert ("restart", None) in worker.commands
        worker.events.put(FrontendEvent("configuration_cleared"))
        await pilot.pause(0.1)
        assert app.query_one("#menu-bar").display
        assert app.query_one(GameViewport).mouse_enabled
        assert app.query_one("#file-project-settings", Button).disabled


async def test_packaged_project_preferences_apply_only_hot_session_settings(
    tmp_path: Path,
) -> None:
    app = RustyEraTui(tmp_path, None)
    worker = FakeWorker()
    app.worker = worker  # type: ignore[assignment]
    snapshot = ConfigurationSnapshot.from_wire(
        {
            0: 10,
            1: b"package",
            2: [
                configuration_entry("UseMouse", "YES", 0),
                configuration_entry("BackColor", "0,0,0", 4),
                configuration_entry("AutoSave", "YES", 0, application=1),
                configuration_entry("SystemSaveInBinary", "NO", 0, application=1),
                configuration_entry("ZipSaveData", "NO", 0, application=1),
            ],
            3: False,
        }
    )

    async with app.run_test(size=(110, 35)) as pilot:
        worker.events.put(FrontendEvent("phase", 4))
        worker.events.put(FrontendEvent("configuration", (snapshot, True)))
        await pilot.pause(0.1)
        app.action_project_settings()
        await pilot.pause()
        assert isinstance(app.screen, ProjectSettingsDialog)
        assert "退出游戏后将丢失" in str(
            app.screen.query_one("#preferences-read-only", Static).render()
        )
        use_mouse = app.screen.query_one("#preference-usemouse", Checkbox)
        auto_save = app.screen.query_one("#preference-autosave", Checkbox)
        assert not use_mouse.disabled
        assert auto_save.disabled
        assert app.screen.query_one("#preferences-apply", Button).disabled
        assert app.screen.query_one("#preferences-apply-restart", Button).disabled

        tabs = app.screen.query_one("#preferences-tabs", TabbedContent)
        tabs.active = "preferences-save"
        await pilot.pause()
        assert app.screen.query_one("#preferences-reset", Button).disabled
        tabs.active = "preferences-interface"
        await pilot.pause()

        use_mouse.value = False
        back_color = app.screen.query_one("#preference-backcolor", ColorField)
        back_color.value = "1,2,3"
        await pilot.pause()
        assert not app.screen.query_one("#preferences-apply", Button).disabled
        await pilot.click("#preferences-apply")
        await pilot.pause()
        assert (
            "save_configuration",
            (
                [
                    ConfigurationChange("UseMouse", "NO"),
                    ConfigurationChange("BackColor", "1,2,3"),
                ],
                False,
            ),
        ) in worker.commands

        worker.events.put(FrontendEvent("configuration_session_applied"))
        await pilot.pause(0.1)
        assert "退出游戏后将丢失" in str(
            app.screen.query_one("#preferences-status", Static).render()
        )
