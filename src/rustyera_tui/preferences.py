"""Structured project settings editor for the Textual frontend."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TabbedContent, TabPane

from .color_picker import ColorField
from .configuration import (
    APPLICATION_HOT,
    ConfigurationChange,
    ConfigurationEntry,
    ConfigurationSnapshot,
)
from .preferences_schema import FIELDS, PAGES, FieldSpec, PageSpec
from .preferences_values import _field_id, control_value, set_control_value


class PreferenceField(Horizontal):
    def __init__(self, spec: FieldSpec, entry: ConfigurationEntry, editable: bool) -> None:
        classes = f"preference-field preference-{spec.kind}"
        if spec.wide:
            classes += " preference-wide"
        super().__init__(id=f"row-{_field_id(spec.code)}", classes=classes)
        self.spec = spec
        self.entry = entry
        self.control_disabled = not editable

    def compose(self) -> ComposeResult:
        control_id = _field_id(self.spec.code)
        if self.spec.kind == "boolean":
            yield Checkbox(
                self.spec.label,
                value=self.entry.value == "YES",
                compact=True,
                id=control_id,
                disabled=self.control_disabled,
            )
            return
        yield Label(self.spec.label, markup=False, classes="preference-field-label")
        if self.spec.kind == "integer":
            yield Input(
                value=self.entry.value,
                type="integer",
                compact=True,
                id=control_id,
                disabled=self.control_disabled,
            )
        elif self.spec.kind == "select":
            yield Select(
                self.spec.choices,
                value=self.entry.value,
                allow_blank=False,
                compact=True,
                id=control_id,
                disabled=self.control_disabled,
            )
        elif self.spec.kind == "color":
            yield ColorField(self.entry.value, id=control_id, disabled=self.control_disabled)
        else:
            yield Input(
                value=self.entry.value,
                compact=True,
                id=control_id,
                disabled=self.control_disabled,
            )


class PreferencesDialog(ModalScreen[None]):
    """Four-page settings editor backed by the Runtime configuration profile."""

    class ApplyRequested(Message):
        def __init__(self, changes: list[ConfigurationChange], restart: bool) -> None:
            super().__init__()
            self.changes = changes
            self.restart = restart

    def __init__(self, snapshot: ConfigurationSnapshot, read_only: bool) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.read_only = read_only
        self.entries = {entry.code: entry for entry in snapshot.tui_entries}
        self.busy = False

    def _entry_editable(self, entry: ConfigurationEntry) -> bool:
        return not entry.fixed and (not self.read_only or entry.application == APPLICATION_HOT)

    def _page_has_editable_entries(self, page: PageSpec) -> bool:
        for code in page.codes:
            entry = self.entries.get(code)
            if entry is not None and self._entry_editable(entry):
                return True
        return False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog preferences-dialog"):
            yield Label("RustyEra TUI · 项目设置", classes="dialog-title")
            if self.read_only:
                yield Static(
                    "当前运行的是项目文件；无需重启的设置仅对当前会话有效，退出游戏后将丢失。",
                    markup=False,
                    id="preferences-read-only",
                )
            with TabbedContent(initial="preferences-interface", id="preferences-tabs"):
                for page in PAGES:
                    with TabPane(page.title, id=f"preferences-{page.id}"):
                        with VerticalScroll(classes="preferences-page-scroll"):
                            if page.restart_warning:
                                yield Static(
                                    "更改本页的设置可能导致项目无法打开或正常运行",
                                    markup=False,
                                    classes="preferences-warning",
                                )
                                yield Static(
                                    "※本页的所有修改均在重启游戏后才能生效",
                                    markup=False,
                                    classes="preferences-restart-note",
                                )
                            with Grid(classes="preferences-grid"):
                                for group in page.groups:
                                    yield Label(group.title, classes="preferences-group-title")
                                    for field in group.fields:
                                        entry = self.entries.get(field.code)
                                        if entry is not None:
                                            yield PreferenceField(
                                                field, entry, self._entry_editable(entry)
                                            )
            yield Static("", id="preferences-status", markup=False)
            with Horizontal(classes="preferences-actions"):
                yield Button(
                    "恢复本页默认",
                    id="preferences-reset",
                    compact=True,
                    disabled=True,
                )
                yield Static(classes="preferences-action-spacer")
                yield Button(
                    "应用",
                    id="preferences-apply",
                    compact=True,
                    disabled=True,
                )
                yield Button(
                    "应用并重启",
                    id="preferences-apply-restart",
                    variant="primary",
                    compact=True,
                    disabled=self.read_only,
                )
                yield Button(
                    "取消",
                    id="preferences-cancel",
                    compact=True,
                )

    def on_mount(self) -> None:
        self._update_zip_dependency()
        self._update_status()
        self._refresh_action_buttons()

    def _active_page(self) -> PageSpec:
        active = self.query_one("#preferences-tabs", TabbedContent).active
        return next(page for page in PAGES if active == f"preferences-{page.id}")

    def _changes(self) -> list[ConfigurationChange] | None:
        changes: list[ConfigurationChange] = []
        for page in PAGES:
            for code in page.codes:
                entry = self.entries.get(code)
                if entry is None or not self._entry_editable(entry):
                    continue
                field = FIELDS[code]
                try:
                    value = control_value(self.query_one, field)
                except ValueError as error:
                    self.notify(f"{field.label}：{error}", severity="error")
                    return None
                if value != entry.value:
                    changes.append(ConfigurationChange(code, value))
        return changes

    def _update_zip_dependency(self) -> None:
        binary = self.query_one(f"#{_field_id('SystemSaveInBinary')}", Checkbox)
        zip_field = self.query_one(f"#{_field_id('ZipSaveData')}", Checkbox)
        zip_entry = self.entries.get("ZipSaveData")
        zip_field.disabled = (
            not binary.value
            or zip_entry is None
            or not self._entry_editable(zip_entry)
            or self.busy
        )

    def _update_status(self, message: str | None = None) -> None:
        if message is None:
            message = "存在需重启后生效的已保存设置" if self.snapshot.restart_pending else ""
        self.query_one("#preferences-status", Static).update(message)

    def _draft_has_changes(self) -> bool:
        for code, entry in self.entries.items():
            if not self._entry_editable(entry):
                continue
            field = FIELDS.get(code)
            if field is None:
                continue
            try:
                value = control_value(self.query_one, field)
            except ValueError:
                return True
            if value != entry.value:
                return True
        return False

    def _refresh_action_buttons(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#preferences-reset", Button).disabled = self.busy or not (
            self._page_has_editable_entries(self._active_page())
        )
        self.query_one("#preferences-apply", Button).disabled = (
            self.busy or not self._draft_has_changes()
        )
        self.query_one("#preferences-apply-restart", Button).disabled = self.busy or self.read_only

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._update_zip_dependency()
        self._refresh_action_buttons()
        if busy:
            self._update_status("正在保存设置…")

    def replace_snapshot(self, snapshot: ConfigurationSnapshot) -> None:
        self.snapshot = snapshot
        self.entries = {entry.code: entry for entry in snapshot.tui_entries}
        for code, entry in self.entries.items():
            field = FIELDS.get(code)
            if field is not None:
                set_control_value(self.query_one, field, entry.value)
        self.set_busy(False)
        self._update_status()

    def save_failed(self, message: str) -> None:
        self.set_busy(False)
        self._update_status(message)

    def session_applied(self) -> None:
        self.set_busy(False)
        self._update_status("会话设置已应用；退出游戏后将丢失")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == _field_id("SystemSaveInBinary"):
            self._update_zip_dependency()
        self._refresh_action_buttons()

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._refresh_action_buttons()

    def on_select_changed(self, _event: Select.Changed) -> None:
        self._refresh_action_buttons()

    def on_color_field_changed(self, _event: ColorField.Changed) -> None:
        self._refresh_action_buttons()

    def on_tabbed_content_tab_activated(self, _event: TabbedContent.TabActivated) -> None:
        self._refresh_action_buttons()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "preferences-cancel":
            self.dismiss()
            return
        if button_id == "preferences-reset":
            for code in self._active_page().codes:
                entry = self.entries.get(code)
                if entry is not None and self._entry_editable(entry):
                    set_control_value(self.query_one, FIELDS[code], entry.default_value)
            self._update_zip_dependency()
            self._refresh_action_buttons()
            return
        if button_id not in {"preferences-apply", "preferences-apply-restart"}:
            return
        changes = self._changes()
        if changes is None:
            return
        restart = button_id == "preferences-apply-restart"
        if not changes and not restart:
            self._update_status("没有需要应用的更改")
            return
        self.set_busy(True)
        self.post_message(self.ApplyRequested(changes, restart))
