"""Modal filesystem and debugger surfaces used by the TUI menu."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Input,
    Label,
    RichLog,
    Static,
    TextArea,
)


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
        self.query_one("#path-value", Input).value = str(event.path)

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
    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self.lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("Runtime / 前端日志", classes="dialog-title")
            yield TextArea(
                "\n".join(self.lines),
                read_only=True,
                soft_wrap=False,
                show_line_numbers=False,
                id="log-view",
            )
            yield Button("关闭", id="dialog-close")

    def on_mount(self) -> None:
        self.call_after_refresh(
            self.query_one("#log-view", TextArea).scroll_end,
            animate=False,
            immediate=True,
            x_axis=False,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss(None)


class DebugConsoleDialog(ModalScreen[None]):
    class Submitted(Message):
        def __init__(self, source: str, execute: bool) -> None:
            super().__init__()
            self.source = source
            self.execute = execute

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("EraBasic 调试控制台", classes="dialog-title")
            yield RichLog(id="console-output", wrap=True, highlight=True)
            yield Input(placeholder="输入表达式或安全语句", id="console-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("求值", id="console-evaluate", variant="primary")
                yield Button("安全执行", id="console-execute")
                yield Button("关闭", id="dialog-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss(None)
            return
        source = self.query_one("#console-input", Input).value
        if source and event.button.id in ("console-evaluate", "console-execute"):
            self.post_message(self.Submitted(source, event.button.id == "console-execute"))

    def write(self, value: Any) -> None:
        self.query_one("#console-output", RichLog).write(value)


class VariableDialog(ModalScreen[None]):
    class ReadRequested(Message):
        def __init__(self, descriptor: dict[int, Any]) -> None:
            super().__init__()
            self.descriptor = descriptor

    def __init__(self) -> None:
        super().__init__()
        self.descriptors: list[dict[int, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("变量查看（调试暂停点）", classes="dialog-title")
            yield Static("正在请求一致的变量列表…", id="variable-status")
            yield DataTable(id="variable-table", zebra_stripes=True)
            with Horizontal(classes="dialog-buttons"):
                yield Button("刷新", id="variables-refresh", variant="primary")
                yield Button("关闭", id="dialog-close")

    def on_mount(self) -> None:
        table = self.query_one("#variable-table", DataTable)
        table.add_columns("名称", "存储", "类型", "维度", "可写")

    def set_variables(self, page: dict[int, Any]) -> None:
        table = self.query_one("#variable-table", DataTable)
        table.clear()
        storage_names = ["全局", "函数静态", "角色", "局部"]
        type_names = ["整数", "字符串", "布尔", "字节"]
        self.descriptors = list(page.get(1, []))
        for index, descriptor in enumerate(self.descriptors):
            table.add_row(
                descriptor.get(1, ""),
                storage_names[descriptor.get(2, 0)],
                type_names[descriptor.get(3, 0)],
                "×".join(str(value) for value in descriptor.get(4, [])) or "标量",
                "是" if descriptor.get(5) else "否",
                key=str(index),
            )
        self.query_one("#variable-status", Static).update(f"共 {len(page.get(1, []))} 个变量")

    def set_value(self, value: dict[int, Any]) -> None:
        self.query_one("#variable-status", Static).update(
            f"当前值：{value.get(1)!r}（revision {value.get(2, 0)}）"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            descriptor = self.descriptors[int(event.row_key.value)]
        except (TypeError, ValueError, IndexError):
            return
        self.post_message(self.ReadRequested(descriptor))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss(None)
        elif event.button.id == "variables-refresh":
            self.app.post_message(VariableRefresh())


class VariableRefresh(Message):
    pass


class StackDialog(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("纤程、调用栈与操作数栈", classes="dialog-title")
            yield Static("正在请求一致的栈视图…", id="stack-status")
            yield DataTable(id="fiber-table", zebra_stripes=True)
            yield DataTable(id="frame-table", zebra_stripes=True)
            yield DataTable(id="operand-table", zebra_stripes=True)
            yield Button("关闭", id="dialog-close")

    def on_mount(self) -> None:
        self.query_one("#fiber-table", DataTable).add_columns("Fiber", "状态", "主纤程", "帧数")
        self.query_one("#frame-table", DataTable).add_columns("Frame", "函数", "指令", "源码")
        self.query_one("#operand-table", DataTable).add_columns("偏移", "值")

    def set_fibers(self, page: dict[int, Any]) -> int | None:
        table = self.query_one("#fiber-table", DataTable)
        table.clear()
        state_names = ["可运行", "等待 Host", "等待恢复", "完成", "故障", "取消", "调试暂停"]
        fibers = page.get(1, [])
        for fiber in fibers:
            table.add_row(
                str(fiber.get(0)),
                state_names[fiber.get(1, 0)],
                "是" if fiber.get(2) else "否",
                str(fiber.get(3, 0)),
            )
        self.query_one("#stack-status", Static).update(f"共 {len(fibers)} 个纤程")
        selected = next((fiber for fiber in fibers if fiber.get(2)), fibers[0] if fibers else None)
        return selected.get(0) if selected else None

    def set_frames(self, stack: dict[int, Any]) -> tuple[int, int] | None:
        table = self.query_one("#frame-table", DataTable)
        table.clear()
        frames = stack.get(2, [])
        for frame in frames:
            source = frame.get(5)
            location = f"{source.get(0)}:{source.get(4)}" if source else ""
            table.add_row(str(frame.get(0)), frame.get(3, ""), str(frame.get(4, 0)), location)
        return (stack.get(1), frames[0].get(0)) if frames else None

    def set_operands(self, page: dict[int, Any]) -> None:
        table = self.query_one("#operand-table", DataTable)
        table.clear()
        for operand in page.get(3, []):
            table.add_row(str(operand.get(0)), repr(operand.get(1)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss(None)
