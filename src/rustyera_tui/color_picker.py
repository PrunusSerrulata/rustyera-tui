"""Numeric and generated-grid color controls for project settings."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


def parse_rgb(value: str) -> tuple[int, int, int]:
    parts = value.split(",")
    if len(parts) != 3:
        raise ValueError("颜色值必须包含三个 RGB 分量")
    rgb = (int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip()))
    if any(component < 0 or component > 255 for component in rgb):
        raise ValueError("RGB 分量必须在 0 到 255 之间")
    return rgb


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{component:02X}" for component in rgb)


def parse_hex(value: str) -> tuple[int, int, int]:
    normalized = value.strip().removeprefix("#")
    if len(normalized) != 6:
        raise ValueError("HEX 颜色必须是六位十六进制数")
    try:
        return (
            int(normalized[0:2], 16),
            int(normalized[2:4], 16),
            int(normalized[4:6], 16),
        )
    except ValueError as error:
        raise ValueError("HEX 颜色包含无效字符") from error


class ColorPickerDialog(ModalScreen[str | None]):
    """Select one of 216 generated colors or enter an exact HEX/RGB value."""

    LEVELS = (0, 51, 102, 153, 204, 255)

    def __init__(self, value: str) -> None:
        super().__init__()
        self.rgb = parse_rgb(value)
        self._syncing = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog color-picker-dialog"):
            yield Label("选择颜色", classes="dialog-title")
            with Horizontal(classes="color-editor-row"):
                yield Label("颜色预览", classes="color-editor-label")
                yield Static("", id="color-preview")
            with Horizontal(classes="color-editor-row"):
                yield Label("HEX", classes="color-editor-label")
                yield Input(value=hex_color(self.rgb), id="color-hex", max_length=7)
            for name, label, component in zip(
                ("red", "green", "blue"),
                ("红色", "绿色", "蓝色"),
                self.rgb,
                strict=True,
            ):
                with Horizontal(classes="color-editor-row"):
                    yield Label(label, classes="color-editor-label")
                    yield Input(value=str(component), type="integer", id=f"color-{name}")
            yield Label("216 色网格", classes="color-grid-title")
            with Grid(id="color-grid"):
                for red in self.LEVELS:
                    for green in self.LEVELS:
                        for blue in self.LEVELS:
                            color = hex_color((red, green, blue))
                            button = Button(" ", id=f"color-cell-{red}-{green}-{blue}")
                            button.styles.background = color
                            yield button
            yield Static("", id="color-error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="color-cancel")
                yield Button("确定", id="color-confirm", variant="primary")

    def on_mount(self) -> None:
        self._sync_fields()

    def _sync_fields(self) -> None:
        self._syncing = True
        try:
            self.query_one("#color-hex", Input).value = hex_color(self.rgb)
            for name, component in zip(("red", "green", "blue"), self.rgb, strict=True):
                self.query_one(f"#color-{name}", Input).value = str(component)
            preview = self.query_one("#color-preview", Static)
            preview.styles.background = hex_color(self.rgb)
            preview.update(hex_color(self.rgb))
            self._show_error("")
        finally:
            self._syncing = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._syncing:
            return
        if event.input.id == "color-hex":
            try:
                self.rgb = parse_hex(event.value)
            except ValueError:
                return
            self._sync_fields()
            return
        if event.input.id not in {"color-red", "color-green", "color-blue"}:
            return
        try:
            self.rgb = self._rgb_components()
        except ValueError:
            return
        self._sync_fields()

    def _show_error(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#color-error", Static).update(message)

    def _component(self, name: str) -> int:
        value = int(self.query_one(f"#color-{name}", Input).value)
        if not 0 <= value <= 255:
            raise ValueError("RGB 分量必须在 0 到 255 之间")
        return value

    def _rgb_components(self) -> tuple[int, int, int]:
        return (
            self._component("red"),
            self._component("green"),
            self._component("blue"),
        )

    @on(Button.Pressed, "#color-cancel")
    def cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#color-confirm")
    def confirm(self, event: Button.Pressed) -> None:
        event.stop()
        try:
            hex_rgb = parse_hex(self.query_one("#color-hex", Input).value)
            rgb = self._rgb_components()
        except ValueError as error:
            self._show_error(str(error))
            return
        if rgb != hex_rgb:
            self._show_error("HEX 与 RGB 分量不一致")
            return
        self.dismiss(",".join(str(component) for component in rgb))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("color-cell-"):
            parts = button_id.removeprefix("color-cell-").split("-")
            if len(parts) != 3:
                return
            self.rgb = (int(parts[0]), int(parts[1]), int(parts[2]))
            self._sync_fields()


class ColorField(Horizontal):
    def __init__(self, value: str, *, id: str, disabled: bool = False) -> None:
        super().__init__(id=id, classes="color-field")
        self.rgb = parse_rgb(value)
        self.control_disabled = disabled

    def compose(self) -> ComposeResult:
        color = hex_color(self.rgb)
        swatch = Static(color, classes="color-swatch")
        swatch.styles.background = color
        yield swatch
        yield Button("选择颜色…", id=f"{self.id}-choose", disabled=self.control_disabled)

    @property
    def value(self) -> str:
        return ",".join(str(component) for component in self.rgb)

    @value.setter
    def value(self, value: str) -> None:
        self.rgb = parse_rgb(value)
        if self.is_mounted:
            swatch = self.query_one(Static)
            swatch.update(hex_color(self.rgb))
            swatch.styles.background = hex_color(self.rgb)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != f"{self.id}-choose":
            return
        event.stop()
        self.app.push_screen(ColorPickerDialog(self.value), self._color_selected)

    def _color_selected(self, value: str | None) -> None:
        if value is not None:
            self.value = value
