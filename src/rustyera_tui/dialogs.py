"""Modal filesystem and debugger surfaces used by the TUI menu."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Rule,
    Select,
    Static,
)

from .log_model import LogEntry, LogLevel, filter_log_entries, format_log_entries
from .dialogs_debug import (
    DebugConsoleDialog as DebugConsoleDialog,
    StackDialog as StackDialog,
    VariableDialog as VariableDialog,
    VariableRefresh as VariableRefresh,
)
from .preferences import PreferencesDialog as PreferencesDialog
from .runtime_types import GameInformation


class ConfirmDialog(ModalScreen[bool]):
    """Ask the user to confirm a destructive frontend action."""

    def __init__(self, title: str, message: str, confirm_label: str = "确定") -> None:
        super().__init__()
        self.dialog_title = title
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog confirm-dialog"):
            yield Label(self.dialog_title, classes="dialog-title")
            yield Static(self.message, id="confirm-message")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.confirm_label, id="confirm-accept", variant="error")
                yield Button("取消", id="confirm-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-accept")


class ExportProgressDialog(ModalScreen[bool]):
    """Show cancellable progress for a user-initiated full project export."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog export-progress-dialog"):
            yield Label("导出全量项目文件", classes="dialog-title")
            yield Static("正在读取项目文件…", id="export-progress-message", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="export-progress-cancel", variant="error")

    def update_progress(self, message: str) -> None:
        self.query_one("#export-progress-message", Static).update(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "export-progress-cancel":
            event.button.disabled = True
            self.update_progress("正在取消导出…")
            self.dismiss(False)


class AboutDialog(ModalScreen[None]):
    """Display build and licensing information for the frontend and bound core."""

    def __init__(
        self,
        frontend_version: str,
        core_version: str,
        game_information: GameInformation | None = None,
    ) -> None:
        super().__init__()
        self.frontend_version = frontend_version
        self.core_version = core_version
        self.game_information = game_information or GameInformation()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog about-dialog"):
            yield Label("关于 RustyEra TUI", classes="dialog-title")
            yield Static("作者：PrunusSerrulata", markup=False)
            yield Static(f"前端版本：{self.frontend_version}", markup=False)
            yield Static(f"core 版本：{self.core_version}", markup=False)
            yield Static("许可证：GPL-3.0-only", markup=False)
            yield Static(
                "仅适用于 RustyEra 相关组件；游戏本体的许可证以其指定的为准。",
                id="about-license-note",
                markup=False,
            )
            yield Static(
                self._repository_link(
                    "core 仓库", "https://github.com/PrunusSerrulata/rustyera-core"
                ),
                id="about-core-repository",
            )
            yield Static(
                self._repository_link(
                    "TUI 仓库", "https://github.com/PrunusSerrulata/rustyera-tui"
                ),
                id="about-tui-repository",
            )
            game_items = self.game_information.display_items()
            if game_items:
                yield Rule(id="about-game-separator")
                yield Label("当前游戏", classes="about-section-title")
                for label, value in game_items:
                    yield Static(f"{label}：{value}", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("确定", id="about-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "about-close":
            self.dismiss()

    @staticmethod
    def _repository_link(label: str, url: str) -> Text:
        line = Text(f"{label}：")
        line.append(url, style=f"link {url}")
        return line


class FatalErrorDialog(ModalScreen[None]):
    """Keep recovery and diagnosis actions available after an unrecoverable runtime fault."""

    class Action(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog fatal-dialog"):
            yield Label("游戏错误", classes="dialog-title fatal-title")
            yield Static("游戏遇到了无法恢复的错误：", markup=False, classes="fatal-description")
            yield Static(self.error, markup=False, id="fatal-error")
            yield Static(
                "您可以将此错误和诊断信息发送给该游戏项目的开发者和 RustyEra 的开发者，"
                "以更好地帮助他们解决错误。",
                markup=False,
                classes="fatal-description",
            )
            yield Static(
                "诊断信息将包含故障时的 VM 快照、日志与编译产物。",
                markup=False,
                id="fatal-export-status",
            )
            yield ProgressBar(
                total=None,
                show_eta=False,
                id="fatal-export-progress",
            )
            with Horizontal(classes="dialog-buttons fatal-buttons"):
                yield Button("导出诊断信息…", id="fatal-export", variant="primary")
                yield Static(classes="fatal-buttons-spacer")
                yield Button("返回主菜单", id="fatal-title")
                yield Button("重启并重新编译", id="fatal-recompile")
                yield Button("退出", id="fatal-exit", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = {
            "fatal-export": "export",
            "fatal-title": "title",
            "fatal-recompile": "recompile",
            "fatal-exit": "exit",
        }.get(event.button.id or "")
        if action is not None:
            self.post_message(self.Action(action))

    def set_exporting(self, message: str = "正在准备诊断信息…") -> None:
        self.query_one("#fatal-export-status", Static).update(message)
        self.query_one("#fatal-export-progress", ProgressBar).add_class("visible")
        self._set_buttons_disabled(True)

    def update_export_progress(self, message: str, completed: int, total: int) -> None:
        self.set_exporting(message)
        self.query_one("#fatal-export-progress", ProgressBar).update(
            progress=completed,
            total=total if total > 0 else None,
        )

    def finish_export(self, success: bool, message: str) -> None:
        status = f"导出成功：{message}" if success else f"导出失败：{message}"
        self.query_one("#fatal-export-status", Static).update(status)
        self.query_one("#fatal-export-progress", ProgressBar).remove_class("visible")
        self._set_buttons_disabled(False)

    def _set_buttons_disabled(self, disabled: bool) -> None:
        for button in self.query(".fatal-buttons Button"):
            button.disabled = disabled


class PathDialog(ModalScreen[Path | None]):
    def __init__(self, title: str, mode: str, initial: Path) -> None:
        super().__init__()
        self.dialog_title = title
        self.mode = mode
        self.initial_value = initial.expanduser().resolve()
        self.initial = self.initial_value
        if self.mode == "save" or self.initial.is_file():
            self.initial = self.initial.parent

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog path-dialog"):
            yield Label(self.dialog_title, classes="dialog-title")
            yield DirectoryTree(self.initial, id="path-tree")
            yield Input(str(self.initial_value), id="path-value")
            with Horizontal(classes="dialog-buttons"):
                yield Button("确定", id="path-accept", variant="primary")
                yield Button("取消", id="path-cancel")

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        value = event.path / self.initial_value.name if self.mode == "save" else event.path
        self.query_one("#path-value", Input).value = str(value)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.query_one("#path-value", Input).value = str(event.path)
        if self.mode == "file":
            self.dismiss(Path(event.path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "path-cancel":
            self.dismiss(None)
            return
        if event.button.id != "path-accept":
            return
        path = Path(self.query_one("#path-value", Input).value).expanduser()
        if self.mode == "directory" and not path.is_dir():
            self.notify("请选择现有文件夹", severity="error")
        elif self.mode == "file" and not path.is_file():
            self.notify("请选择现有文件", severity="error")
        else:
            self.dismiss(path)


class LogDialog(ModalScreen[None]):
    class Action(Message):
        def __init__(self, action: str, contents: str) -> None:
            super().__init__()
            self.action = action
            self.contents = contents

    _THRESHOLDS = {
        "error": LogLevel.ERROR,
        "warning": LogLevel.WARNING,
        "info": LogLevel.INFO,
        "debug": LogLevel.DEBUG,
    }

    def __init__(self, entries: list[LogEntry]) -> None:
        super().__init__()
        self.entries = entries
        self.threshold = LogLevel.INFO

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("Runtime / 前端日志", classes="dialog-title")
            yield RichLog(wrap=False, highlight=False, markup=False, id="log-view")
            with Horizontal(id="log-actions-row", classes="dialog-buttons"):
                yield Label("最低显示等级", id="log-filter-label")
                yield Select(
                    (
                        ("Error", "error"),
                        ("Warning", "warning"),
                        ("Info", "info"),
                        ("Debug", "debug"),
                    ),
                    value="info",
                    allow_blank=False,
                    id="log-level",
                )
                yield Static(id="log-actions-spacer")
                yield Button("复制日志", id="log-copy")
                yield Button("导出日志", id="log-export")
                yield Button("清空日志", id="log-clear")
                yield Static(id="log-close-spacer")
                yield Button("关闭", id="dialog-close")

    def on_mount(self) -> None:
        self._refresh_logs(LogLevel.INFO)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "log-level":
            return
        self.threshold = self._THRESHOLDS.get(str(event.value), LogLevel.INFO)
        self._refresh_logs(self.threshold)

    def _visible_log_text(self) -> str:
        return format_log_entries(filter_log_entries(self.entries, self.threshold))

    def _refresh_logs(self, threshold: LogLevel) -> None:
        view = self.query_one("#log-view", RichLog)
        view.clear()
        for entry in filter_log_entries(self.entries, threshold):
            view.write(entry.render())
        self.call_after_refresh(
            view.scroll_end,
            animate=False,
            immediate=True,
            x_axis=False,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "log-copy":
            contents = self._visible_log_text()
            self.app.copy_to_clipboard(contents)
            self.notify("日志已复制到剪贴板")
        elif event.button.id == "log-export":
            self.post_message(self.Action("export", self._visible_log_text()))
        elif event.button.id == "log-clear":
            self.entries.clear()
            self.query_one("#log-view", RichLog).clear()
        elif event.button.id == "dialog-close":
            self.dismiss(None)


for _dialog_class in (DebugConsoleDialog, VariableDialog, VariableRefresh, StackDialog):
    _dialog_class.__module__ = __name__
DebugConsoleDialog.Submitted.__module__ = __name__
VariableDialog.ReadRequested.__module__ = __name__
StackDialog.Ready.__module__ = __name__
