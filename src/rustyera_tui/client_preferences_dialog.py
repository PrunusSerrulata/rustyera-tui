"""Global and per-project client preference editor."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TabbedContent, TabPane

from .client_preferences import ClientPreferenceValues, LoadedPreferences, PreferenceValues
from .color_picker import ColorField
from .configuration import ConfigurationSnapshot
from .preferences_schema import FIELDS, PAGES, FieldSpec

PREFERENCE_CODES = (
    "UseMouse",
    "ButtonWrap",
    "ForeColor",
    "BackColor",
    "FocusColor",
    "ReplaceFullWidthSpaces",
)
DEFAULTS = {
    "UseMouse": "YES",
    "ButtonWrap": "NO",
    "ForeColor": "192,192,192",
    "BackColor": "0,0,0",
    "FocusColor": "255,255,0",
    "ReplaceFullWidthSpaces": "NO",
}
CLIENT_DEFAULTS = {
    "imageScale": 1.0,
    "masterVolume": 1.0,
    "trustProjectFileMetadata": False,
}
PREFERENCE_GROUPS = tuple(
    (
        group.title,
        tuple(field for field in group.fields if field.code in PREFERENCE_CODES),
    )
    for group in PAGES[0].groups
    if any(field.code in PREFERENCE_CODES for field in group.fields)
)


class PreferenceOverrideField(Horizontal):
    def __init__(
        self,
        scope: str,
        spec: FieldSpec,
        value: str,
        overridden: bool,
        disabled: bool,
    ) -> None:
        classes = f"preference-field preference-override-field preference-{spec.kind}"
        if spec.wide:
            classes += " preference-wide"
        super().__init__(id=f"row-preference-{scope}-{spec.code.lower()}", classes=classes)
        self.scope = scope
        self.spec = spec
        self.value = value
        self.overridden = overridden
        self.control_disabled = disabled

    def compose(self) -> ComposeResult:
        yield Checkbox(
            "覆盖",
            value=self.overridden,
            compact=True,
            id=f"preference-{self.scope}-override-{self.spec.code.lower()}",
            classes="preference-override-toggle",
            disabled=self.control_disabled,
        )
        if self.spec.kind == "boolean":
            yield Checkbox(
                self.spec.label,
                value=self.value == "YES",
                compact=True,
                id=f"preference-{self.scope}-{self.spec.code.lower()}",
                classes="preference-value-toggle",
                disabled=self.control_disabled or not self.overridden,
            )
        elif self.spec.kind == "color":
            yield Label(self.spec.label, markup=False, classes="preference-field-label")
            yield ColorField(
                self.value,
                id=f"preference-{self.scope}-{self.spec.code.lower()}",
                disabled=self.control_disabled or not self.overridden,
            )


class ClientPreferenceOverrideField(Horizontal):
    def __init__(
        self,
        scope: str,
        key: str,
        label: str,
        kind: str,
        disabled: bool,
    ) -> None:
        super().__init__(classes="preference-field preference-override-field")
        self.scope = scope
        self.key = key
        self.label = label
        self.kind = kind
        self.control_disabled = disabled

    def compose(self) -> ComposeResult:
        key = self.key.lower()
        yield Checkbox(
            "覆盖",
            compact=True,
            id=f"preference-{self.scope}-client-override-{key}",
            classes="preference-override-toggle",
            disabled=self.control_disabled,
        )
        if self.kind == "boolean":
            yield Checkbox(
                self.label,
                compact=True,
                id=f"preference-{self.scope}-client-{key}",
                classes="preference-value-toggle",
                disabled=True,
            )
        else:
            yield Label(self.label, markup=False, classes="preference-field-label")
            yield Input(
                id=f"preference-{self.scope}-client-{key}",
                type="number",
                compact=True,
                disabled=True,
            )


class PreferencesDialog(ModalScreen[None]):
    """Edit sparse global and current-project TUI preference overrides."""

    class SaveRequested(Message):
        def __init__(self, scope: str, values: PreferenceValues) -> None:
            super().__init__()
            self.scope = scope
            self.values = values

    def __init__(
        self,
        snapshot: ConfigurationSnapshot | None,
        global_preferences: LoadedPreferences,
        project_preferences: LoadedPreferences | None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.loaded = {"global": global_preferences, "project": project_preferences}
        self.drafts = {
            "global": dict(global_preferences.values.settings),
            "project": dict(project_preferences.values.settings)
            if project_preferences is not None
            else {},
        }
        self.client_drafts = {
            "global": global_preferences.values.client,
            "project": project_preferences.values.client
            if project_preferences is not None
            else ClientPreferenceValues(),
        }
        self.scope = "global"
        self.busy = False

    def _inherited(self, code: str) -> str:
        if self.snapshot is None:
            return DEFAULTS[code]
        return self.snapshot.client_effective_value(code, DEFAULTS[code])

    def _scope_read_only(self) -> bool:
        loaded = self.loaded[self.scope]
        return loaded is None or loaded.read_only

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog preferences-dialog"):
            yield Label("RustyEra TUI · 偏好设置", classes="dialog-title")
            with TabbedContent(initial="preferences-global", id="preferences-tabs"):
                for scope, title in (("global", "全局偏好"), ("project", "项目偏好")):
                    loaded = self.loaded[scope]
                    if loaded is None:
                        continue
                    with TabPane(title, id=f"preferences-{scope}"):
                        with VerticalScroll(classes="preferences-page-scroll"):
                            yield Static(
                                "项目偏好优先于项目设置；全局偏好仅在项目没有明确设置时生效。",
                                markup=False,
                                classes="preferences-scope-note",
                            )
                            yield Static(
                                loaded.error or "",
                                markup=False,
                                classes="preferences-read-only",
                                id=f"preferences-{scope}-read-only",
                            )
                            with Grid(classes="preferences-grid"):
                                for group_title, fields in PREFERENCE_GROUPS:
                                    yield Label(group_title, classes="preferences-group-title")
                                    for field in fields:
                                        draft = self.drafts[scope]
                                        yield PreferenceOverrideField(
                                            scope,
                                            field,
                                            draft.get(field.code, self._inherited(field.code)),
                                            field.code in draft,
                                            loaded.read_only,
                                        )
                                yield Label(
                                    "客户端显示与项目加载", classes="preferences-group-title"
                                )
                                yield ClientPreferenceOverrideField(
                                    scope, "imageScale", "图片缩放", "number", loaded.read_only
                                )
                                yield ClientPreferenceOverrideField(
                                    scope, "masterVolume", "主音量", "number", loaded.read_only
                                )
                                yield ClientPreferenceOverrideField(
                                    scope,
                                    "trustProjectFileMetadata",
                                    "信任文件大小和修改时间",
                                    "boolean",
                                    loaded.read_only,
                                )
            yield Static("", id="preferences-status", markup=False)
            with Horizontal(classes="preferences-actions"):
                yield Button("恢复本范围默认", id="preferences-reset", compact=True)
                yield Static(classes="preferences-action-spacer")
                yield Button("应用", id="preferences-apply", variant="primary", compact=True)
                yield Button("取消", id="preferences-cancel", compact=True)

    def on_mount(self) -> None:
        for scope, loaded in self.loaded.items():
            if loaded is not None:
                self._refresh_scope(scope)
        self._refresh_action_buttons()

    def _active_scope(self) -> str:
        active = self.query_one("#preferences-tabs", TabbedContent).active
        return "project" if active == "preferences-project" else "global"

    def _capture(self, scope: str) -> bool:
        draft: dict[str, str] = {}
        for code in PREFERENCE_CODES:
            override = self.query_one(f"#preference-{scope}-override-{code.lower()}", Checkbox)
            if not override.value:
                continue
            try:
                draft[code] = self._control_value(scope, FIELDS[code])
            except ValueError as error:
                self.notify(f"{FIELDS[code].label}：{error}", severity="error")
                return False
        self.drafts[scope] = draft
        image_scale: float | None = None
        master_volume: float | None = None
        for key in ("imageScale", "masterVolume"):
            override = self.query_one(
                f"#preference-{scope}-client-override-{key.lower()}", Checkbox
            )
            if not override.value:
                continue
            try:
                value = float(
                    self.query_one(f"#preference-{scope}-client-{key.lower()}", Input).value
                )
            except ValueError:
                self.notify(f"{key} 必须是数字", severity="error")
                return False
            minimum, maximum = (0.25, 4.0) if key == "imageScale" else (0.0, 1.0)
            if not minimum <= value <= maximum:
                self.notify(f"{key} 必须在 {minimum} 到 {maximum} 之间", severity="error")
                return False
            if key == "imageScale":
                image_scale = value
            else:
                master_volume = value
        trust_override = self.query_one(
            f"#preference-{scope}-client-override-trustprojectfilemetadata", Checkbox
        )
        trust = None
        if trust_override.value:
            trust = self.query_one(
                f"#preference-{scope}-client-trustprojectfilemetadata", Checkbox
            ).value
        self.client_drafts[scope] = ClientPreferenceValues(image_scale, master_volume, trust)
        return True

    def _control_value(self, scope: str, field: FieldSpec) -> str:
        if field.kind == "boolean":
            control = self.query_one(f"#preference-{scope}-{field.code.lower()}", Checkbox)
            return "YES" if control.value else "NO"
        if field.kind == "color":
            return self.query_one(f"#preference-{scope}-{field.code.lower()}", ColorField).value
        raise ValueError(f"不支持的偏好控件类型：{field.kind}")

    def _set_control_value(self, scope: str, field: FieldSpec, value: str) -> None:
        if field.kind == "boolean":
            self.query_one(f"#preference-{scope}-{field.code.lower()}", Checkbox).value = (
                value == "YES"
            )
        elif field.kind == "color":
            self.query_one(f"#preference-{scope}-{field.code.lower()}", ColorField).value = value
        else:
            raise ValueError(f"不支持的偏好控件类型：{field.kind}")

    def _refresh_scope(self, scope: str) -> None:
        draft = self.drafts[scope]
        loaded = self.loaded[scope]
        if loaded is None:
            return
        locked = loaded.read_only or self.busy
        for code in PREFERENCE_CODES:
            overridden = code in draft
            override = self.query_one(f"#preference-{scope}-override-{code.lower()}", Checkbox)
            override.value = overridden
            override.disabled = locked
            self._set_control_value(scope, FIELDS[code], draft.get(code, self._inherited(code)))
            self.query_one(f"#preference-{scope}-{code.lower()}").disabled = (
                locked or not overridden
            )
        client = self.client_drafts[scope].to_json()
        for key in ("imageScale", "masterVolume"):
            overridden = key in client
            self.query_one(
                f"#preference-{scope}-client-override-{key.lower()}", Checkbox
            ).value = overridden
            self.query_one(
                f"#preference-{scope}-client-override-{key.lower()}", Checkbox
            ).disabled = locked
            input_widget = self.query_one(f"#preference-{scope}-client-{key.lower()}", Input)
            input_widget.value = str(client.get(key, CLIENT_DEFAULTS[key]))
            input_widget.disabled = locked or not overridden
        trust_overridden = "trustProjectFileMetadata" in client
        trust_override = self.query_one(
            f"#preference-{scope}-client-override-trustprojectfilemetadata", Checkbox
        )
        trust_override.value = trust_overridden
        trust_override.disabled = locked
        trust = self.query_one(f"#preference-{scope}-client-trustprojectfilemetadata", Checkbox)
        trust.value = bool(
            client.get("trustProjectFileMetadata", CLIENT_DEFAULTS["trustProjectFileMetadata"])
        )
        trust.disabled = locked or not trust_overridden

    def _refresh_action_buttons(self) -> None:
        if not self.is_mounted:
            return
        read_only = self._scope_read_only()
        self.query_one("#preferences-reset", Button).disabled = read_only or self.busy
        self.query_one("#preferences-apply", Button).disabled = read_only or self.busy

    def on_tabbed_content_tab_activated(self, _event: TabbedContent.TabActivated) -> None:
        self.scope = self._active_scope()
        self._refresh_action_buttons()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        widget_id = event.checkbox.id or ""
        scope = self._active_scope()
        client_prefix = f"preference-{scope}-client-override-"
        if widget_id.startswith(client_prefix):
            key = widget_id[len(client_prefix) :]
            self.query_one(f"#preference-{scope}-client-{key}").disabled = (
                self._scope_read_only() or not event.checkbox.value or self.busy
            )
            return
        prefix = f"preference-{scope}-override-"
        if not widget_id.startswith(prefix):
            return
        code_lower = widget_id[len(prefix) :]
        code = next(item for item in PREFERENCE_CODES if item.lower() == code_lower)
        self.query_one(f"#preference-{scope}-{code.lower()}").disabled = (
            self._scope_read_only() or not event.checkbox.value or self.busy
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preferences-cancel":
            self.dismiss()
            return
        if event.button.id == "preferences-reset":
            scope = self._active_scope()
            self.drafts[scope] = {}
            self.client_drafts[scope] = ClientPreferenceValues()
            self._refresh_scope(scope)
            return
        scope = self._active_scope()
        if event.button.id != "preferences-apply" or not self._capture(scope):
            return
        self.busy = True
        for candidate, loaded in self.loaded.items():
            if loaded is not None:
                self._refresh_scope(candidate)
        self._refresh_action_buttons()
        self.query_one("#preferences-status", Static).update("正在保存偏好…")
        self.post_message(
            self.SaveRequested(
                scope,
                PreferenceValues(self.drafts[scope], self.client_drafts[scope]),
            )
        )

    def save_finished(self, message: str) -> None:
        self.busy = False
        for scope, loaded in self.loaded.items():
            if loaded is not None:
                self._refresh_scope(scope)
        self._refresh_action_buttons()
        self.query_one("#preferences-status", Static).update(message)
