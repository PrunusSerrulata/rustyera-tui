"""Global and per-project client preference editor."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from .client_preferences import ClientPreferenceValues, LoadedPreferences, PreferenceValues
from .color_picker import ColorField
from .configuration import ConfigurationSnapshot
from .preferences_schema import FIELDS, FieldSpec
from .preferences_values import _field_id, control_value, set_control_value

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


class PreferenceOverrideField(Horizontal):
    def __init__(self, spec: FieldSpec, value: str, overridden: bool, disabled: bool) -> None:
        classes = f"preference-field preference-{spec.kind}"
        if spec.wide:
            classes += " preference-wide"
        super().__init__(classes=classes)
        self.spec = spec
        self.value = value
        self.overridden = overridden
        self.control_disabled = disabled

    def compose(self) -> ComposeResult:
        yield Checkbox(
            "覆盖",
            value=self.overridden,
            compact=True,
            id=f"preference-override-{self.spec.code.lower()}",
            disabled=self.control_disabled,
        )
        if self.spec.kind == "boolean":
            yield Checkbox(
                self.spec.label,
                value=self.value == "YES",
                compact=True,
                id=_field_id(self.spec.code),
                disabled=self.control_disabled or not self.overridden,
            )
        elif self.spec.kind == "color":
            yield Label(self.spec.label, markup=False, classes="preference-field-label")
            yield ColorField(
                self.value,
                id=_field_id(self.spec.code),
                disabled=self.control_disabled or not self.overridden,
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
        scopes = [("全局", "global")]
        if self.loaded["project"] is not None:
            scopes.append(("当前项目", "project"))
        with Vertical(classes="dialog preferences-dialog"):
            yield Label("RustyEra TUI · 偏好设置", classes="dialog-title")
            yield Select(
                scopes, value="global", allow_blank=False, compact=True, id="preference-scope"
            )
            yield Static("", id="preferences-read-only", markup=False)
            with VerticalScroll(classes="preferences-page-scroll"):
                with Grid(classes="preferences-grid"):
                    yield Label("界面与交互", classes="preferences-group-title")
                    for code in PREFERENCE_CODES:
                        draft = self.drafts["global"]
                        yield PreferenceOverrideField(
                            FIELDS[code],
                            draft.get(code, self._inherited(code)),
                            code in draft,
                            self._scope_read_only(),
                        )
                    yield Label("客户端", classes="preferences-group-title")
                    for key, label in (
                        ("imageScale", "图片缩放"),
                        ("masterVolume", "主音量"),
                    ):
                        yield Checkbox(
                            f"覆盖{label}",
                            id=f"preference-client-override-{key.lower()}",
                        )
                        yield Input(id=f"preference-client-{key.lower()}", type="number")
                    yield Checkbox(
                        "覆盖快速启动文件元数据策略",
                        id="preference-client-override-trustprojectfilemetadata",
                    )
                    yield Checkbox(
                        "信任文件大小和修改时间",
                        id="preference-client-trustprojectfilemetadata",
                    )
            yield Static("", id="preferences-status", markup=False)
            with Horizontal(classes="preferences-actions"):
                yield Button("清除本范围覆盖", id="preferences-reset", compact=True)
                yield Static(classes="preferences-action-spacer")
                yield Button("应用", id="preferences-apply", variant="primary", compact=True)
                yield Button("取消", id="preferences-cancel", compact=True)

    def on_mount(self) -> None:
        self._refresh_scope()

    def _capture(self) -> bool:
        draft: dict[str, str] = {}
        for code in PREFERENCE_CODES:
            override = self.query_one(f"#preference-override-{code.lower()}", Checkbox)
            if not override.value:
                continue
            try:
                draft[code] = control_value(self.query_one, FIELDS[code])
            except ValueError as error:
                self.notify(f"{FIELDS[code].label}：{error}", severity="error")
                return False
        self.drafts[self.scope] = draft
        image_scale: float | None = None
        master_volume: float | None = None
        for key in ("imageScale", "masterVolume"):
            override = self.query_one(f"#preference-client-override-{key.lower()}", Checkbox)
            if not override.value:
                continue
            try:
                value = float(self.query_one(f"#preference-client-{key.lower()}", Input).value)
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
            "#preference-client-override-trustprojectfilemetadata", Checkbox
        )
        trust = None
        if trust_override.value:
            trust = self.query_one("#preference-client-trustprojectfilemetadata", Checkbox).value
        self.client_drafts[self.scope] = ClientPreferenceValues(image_scale, master_volume, trust)
        return True

    def _refresh_scope(self) -> None:
        draft = self.drafts[self.scope]
        read_only = self._scope_read_only()
        loaded = self.loaded[self.scope]
        warning = loaded.error if loaded is not None and loaded.error else ""
        self.query_one("#preferences-read-only", Static).update(warning)
        for code in PREFERENCE_CODES:
            overridden = code in draft
            self.query_one(f"#preference-override-{code.lower()}", Checkbox).value = overridden
            set_control_value(self.query_one, FIELDS[code], draft.get(code, self._inherited(code)))
            self.query_one(f"#{_field_id(code)}").disabled = read_only or not overridden
            self.query_one(f"#preference-override-{code.lower()}", Checkbox).disabled = read_only
        client = self.client_drafts[self.scope].to_json()
        for key in ("imageScale", "masterVolume"):
            overridden = key in client
            self.query_one(
                f"#preference-client-override-{key.lower()}", Checkbox
            ).value = overridden
            input_widget = self.query_one(f"#preference-client-{key.lower()}", Input)
            input_widget.value = str(client.get(key, CLIENT_DEFAULTS[key]))
            input_widget.disabled = read_only or not overridden
        trust_overridden = "trustProjectFileMetadata" in client
        self.query_one(
            "#preference-client-override-trustprojectfilemetadata", Checkbox
        ).value = trust_overridden
        trust = self.query_one("#preference-client-trustprojectfilemetadata", Checkbox)
        trust.value = bool(
            client.get("trustProjectFileMetadata", CLIENT_DEFAULTS["trustProjectFileMetadata"])
        )
        trust.disabled = read_only or not trust_overridden
        self.query_one("#preferences-reset", Button).disabled = read_only or self.busy
        self.query_one("#preferences-apply", Button).disabled = read_only or self.busy

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "preference-scope" or event.value not in {"global", "project"}:
            return
        if not self._capture():
            event.select.value = self.scope
            return
        self.scope = str(event.value)
        self._refresh_scope()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        widget_id = event.checkbox.id or ""
        client_prefix = "preference-client-override-"
        if widget_id.startswith(client_prefix):
            key = widget_id[len(client_prefix) :]
            self.query_one(f"#preference-client-{key}").disabled = (
                self._scope_read_only() or not event.checkbox.value or self.busy
            )
            return
        prefix = "preference-override-"
        if not widget_id.startswith(prefix):
            return
        code_lower = widget_id[len(prefix) :]
        code = next(item for item in PREFERENCE_CODES if item.lower() == code_lower)
        self.query_one(f"#{_field_id(code)}").disabled = (
            self._scope_read_only() or not event.checkbox.value or self.busy
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preferences-cancel":
            self.dismiss()
            return
        if event.button.id == "preferences-reset":
            self.drafts[self.scope] = {}
            self.client_drafts[self.scope] = ClientPreferenceValues()
            self._refresh_scope()
            return
        if event.button.id != "preferences-apply" or not self._capture():
            return
        self.busy = True
        self._refresh_scope()
        self.query_one("#preferences-status", Static).update("正在保存偏好…")
        self.post_message(
            self.SaveRequested(
                self.scope,
                PreferenceValues(self.drafts[self.scope], self.client_drafts[self.scope]),
            )
        )

    def save_finished(self, message: str) -> None:
        self.busy = False
        self._refresh_scope()
        self.query_one("#preferences-status", Static).update(message)
