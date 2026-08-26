"""Private debugger modal surfaces re-exported by the dialogs facade."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static

from .text_budget import bounded_repr, truncate_utf8

MAX_DEBUG_CONSOLE_LINES = 4_096
MAX_DEBUG_FIBERS = 8_192
MAX_DEBUG_VARIABLES = 8_192
MAX_DEBUG_FRAMES = 8_192
MAX_DEBUG_VALUE_BYTES = 1024 * 1024


class DebugConsoleDialog(ModalScreen[None]):
    class Submitted(Message):
        def __init__(self, source: str, execute: bool) -> None:
            super().__init__()
            self.source = source
            self.execute = execute

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("EraBasic 调试控制台", classes="dialog-title")
            yield RichLog(
                id="console-output",
                wrap=True,
                highlight=True,
                max_lines=MAX_DEBUG_CONSOLE_LINES,
            )
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
        rendered = (
            truncate_utf8(value, MAX_DEBUG_VALUE_BYTES)
            if isinstance(value, str)
            else bounded_repr(value, MAX_DEBUG_VALUE_BYTES)
        )
        self.query_one("#console-output", RichLog).write(rendered)


class VariableDialog(ModalScreen[None]):
    class ReadRequested(Message):
        def __init__(self, descriptor: dict[int, Any]) -> None:
            super().__init__()
            self.descriptor = descriptor

    def __init__(self) -> None:
        super().__init__()
        self.descriptors: list[dict[int, Any]] = []
        self.pending_value_row: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("变量查看（调试暂停点）", classes="dialog-title")
            yield Static("正在请求一致的变量列表…", id="variable-status")
            yield DataTable(id="variable-table", zebra_stripes=True, cursor_type="row")
            with Horizontal(classes="dialog-buttons"):
                yield Button("刷新", id="variables-refresh", variant="primary")
                yield Button("关闭", id="dialog-close")

    def on_mount(self) -> None:
        table = self.query_one("#variable-table", DataTable)
        table.add_columns("名称", "存储", "类型", "维度", "可写", "值")

    def set_variables(self, page: dict[int, Any]) -> None:
        table = self.query_one("#variable-table", DataTable)
        table.clear()
        self.pending_value_row = None
        storage_names = ["全局", "函数静态", "角色", "局部"]
        type_names = ["整数", "字符串", "布尔", "字节"]
        self.descriptors = list(page.get(1, [])[:MAX_DEBUG_VARIABLES])
        for index, descriptor in enumerate(self.descriptors):
            table.add_row(
                descriptor.get(1, ""),
                storage_names[descriptor.get(2, 0)],
                type_names[descriptor.get(3, 0)],
                "×".join(str(value) for value in descriptor.get(4, [])) or "标量",
                "是" if descriptor.get(5) else "否",
                "选择查看",
                key=str(index),
            )
        total = len(page.get(1, []))
        suffix = "（已截断）" if total > len(self.descriptors) else ""
        self.query_one("#variable-status", Static).update(
            f"共载入 {len(self.descriptors)} 个变量{suffix}"
        )

    def set_value(self, value: dict[int, Any]) -> None:
        rendered = _debug_value_text(value.get(1))
        if self.pending_value_row is not None:
            self.query_one("#variable-table", DataTable).update_cell_at(
                Coordinate(self.pending_value_row, 5),
                rendered,
            )
        reference = value.get(0, {})
        indices = reference.get(6, [])
        suffix = f"[{', '.join(str(index) for index in indices)}]" if indices else ""
        self.query_one("#variable-status", Static).update(
            f"当前值{suffix}：{rendered}（revision {value.get(2, 0)}）"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            descriptor = self.descriptors[int(event.row_key.value)]
        except (TypeError, ValueError, IndexError):
            return
        self.pending_value_row = int(event.row_key.value)
        self.query_one("#variable-status", Static).update(
            f"正在读取 {descriptor.get(1, '')} 的当前值…"
        )
        self.post_message(self.ReadRequested(descriptor))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss(None)
        elif event.button.id == "variables-refresh":
            self.app.post_message(VariableRefresh())


class VariableRefresh(Message):
    pass


def _debug_value_text(value: Any) -> str:
    try:
        tag, fields = value
        field = fields[0]
    except (TypeError, ValueError, IndexError):
        return bounded_repr(value, MAX_DEBUG_VALUE_BYTES)
    if tag == 0:
        return bounded_repr(field, MAX_DEBUG_VALUE_BYTES)
    if tag == 1:
        return bounded_repr(field, MAX_DEBUG_VALUE_BYTES)
    if tag == 2:
        return "true" if field else "false"
    if tag == 3:
        if isinstance(field, bytes):
            maximum_source_bytes = (MAX_DEBUG_VALUE_BYTES - 3) // 2
            suffix = "…" if len(field) > maximum_source_bytes else ""
            return field[:maximum_source_bytes].hex() + suffix
        return bounded_repr(field, MAX_DEBUG_VALUE_BYTES)
    if tag == 4:
        return "<place>"
    return bounded_repr(value, MAX_DEBUG_VALUE_BYTES)


class StackDialog(ModalScreen[None]):
    class Ready(Message):
        """The stack tables are mounted and ready to receive debugger data."""

    def __init__(self) -> None:
        super().__init__()
        self.fibers: list[dict[int, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide-dialog"):
            yield Label("纤程与调用栈", classes="dialog-title")
            yield Static("正在请求一致的栈视图…", id="stack-status")
            yield DataTable(id="fiber-table", zebra_stripes=True)
            yield DataTable(id="frame-table", zebra_stripes=True)
            yield Button("关闭", id="dialog-close")

    def on_mount(self) -> None:
        self.query_one("#fiber-table", DataTable).add_columns("Fiber", "状态", "主纤程", "帧数")
        self.query_one("#frame-table", DataTable).add_columns("Frame", "函数", "指令", "源码")
        self.post_message(self.Ready())

    def set_fibers(self, page: dict[int, Any]) -> tuple[int | None, int | None]:
        table = self.query_one("#fiber-table", DataTable)
        state_names = ["可运行", "等待 Host", "等待恢复", "完成", "故障", "取消", "调试暂停"]
        remaining = max(0, MAX_DEBUG_FIBERS - len(self.fibers))
        fibers = page.get(1, [])[:remaining]
        self.fibers.extend(fibers)
        for fiber in fibers:
            table.add_row(
                str(fiber.get(0)),
                state_names[fiber.get(1, 0)],
                "是" if fiber.get(2) else "否",
                str(fiber.get(3, 0)),
            )
        next_cursor = page.get(2) if len(self.fibers) < MAX_DEBUG_FIBERS else None
        status = (
            "当前无活动纤程"
            if not self.fibers and next_cursor is None
            else f"已载入 {len(self.fibers)} 个纤程"
        )
        self.query_one("#stack-status", Static).update(status)
        selected = next(
            (fiber for fiber in self.fibers if fiber.get(2) and fiber.get(3, 0) > 0),
            None,
        )
        if selected is None and next_cursor is None:
            selected = next(
                (fiber for fiber in self.fibers if fiber.get(3, 0) > 0),
                next((fiber for fiber in self.fibers if fiber.get(2)), None),
            )
        return (selected.get(0) if selected else None, next_cursor)

    def set_frames(self, stack: dict[int, Any]) -> None:
        table = self.query_one("#frame-table", DataTable)
        table.clear()
        frames = stack.get(2, [])[:MAX_DEBUG_FRAMES]
        for frame in frames:
            source = frame.get(5)
            location = f"{source.get(0)}:{source.get(4)}" if source else ""
            table.add_row(str(frame.get(0)), frame.get(3, ""), str(frame.get(4, 0)), location)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss(None)
