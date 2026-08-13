from __future__ import annotations

import queue
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    ProgressBar,
    RichLog,
    Rule,
    Select,
    Static,
    TabbedContent,
)

from rustyera_tui.app import CORE_VERSION, RustyEraTui
from rustyera_tui.configuration import ConfigurationChange, ConfigurationSnapshot
from rustyera_tui.dialogs import (
    AboutDialog,
    ConfirmDialog,
    ExportProgressDialog,
    FatalErrorDialog,
    PathDialog,
    PreferencesDialog,
)
from rustyera_tui.log_model import LogLevel, LogMessage
from rustyera_tui.presentation import (
    ColumnCellLayout,
    DisplayLineModel,
    DisplaySegment,
    SeparatorLayout,
    parse_line,
)
from rustyera_tui.color_picker import ColorField, ColorPickerDialog
from rustyera_tui.preferences_schema import FIELDS, PAGES, FieldSpec
from rustyera_tui.runtime import DiagnosisProgress, FrontendEvent, PresentationBatch, RuntimeFailure
from rustyera_tui.runtime_types import GameInformation
from rustyera_tui.widgets import GameLine, GameViewport
from rustyera_tui.wire import variant

from erafl_layout_fixture import GUILD_TASK_TOKEN, erafl_guild_line

__all__ = [
    "AboutDialog",
    "Any",
    "Button",
    "CORE_VERSION",
    "Checkbox",
    "ColorField",
    "ColorPickerDialog",
    "ColumnCellLayout",
    "ConfigurationChange",
    "ConfigurationSnapshot",
    "ConfirmDialog",
    "DataTable",
    "DiagnosisProgress",
    "DisplayLineModel",
    "DisplaySegment",
    "ExportProgressDialog",
    "FIELDS",
    "FakeWorker",
    "FatalErrorDialog",
    "FrontendEvent",
    "GUILD_TASK_TOKEN",
    "GameInformation",
    "GameLine",
    "GameViewport",
    "Input",
    "LogLevel",
    "LogMessage",
    "PAGES",
    "Path",
    "PathDialog",
    "PreferencesDialog",
    "PresentationBatch",
    "ProgressBar",
    "RichLog",
    "Rule",
    "RuntimeFailure",
    "RustyEraTui",
    "Select",
    "SeparatorLayout",
    "Static",
    "TabbedContent",
    "Text",
    "cell_len",
    "configuration_entry",
    "configuration_value",
    "datetime",
    "erafl_guild_line",
    "events",
    "parse_line",
    "pytest",
    "variant",
]


class FakeWorker:
    def __init__(self) -> None:
        self.events: queue.Queue[Any] = queue.Queue()
        self.commands: list[tuple[str, Any]] = []
        self.started = False
        self.ident: int | None = None
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.started = True
        self.ident = 1

    def is_alive(self) -> bool:
        return self.started

    def send(self, kind: str, value: Any = None) -> None:
        self.commands.append((kind, value))

    def stop(self) -> None:
        self.started = False

    def join(self, timeout: float | None = None) -> None:
        del timeout

    def shutdown(self) -> None:
        self.stop()


def configuration_entry(
    code: str,
    value: str,
    kind: int,
    *,
    fixed: bool = False,
    default: str | None = None,
    effective: str | None = None,
    application: int = 0,
) -> dict[int, Any]:
    return {
        0: code,
        1: code,
        2: code,
        3: value,
        4: kind,
        5: [],
        6: fixed,
        7: 2,
        8: value if default is None else default,
        9: value if effective is None else effective,
        10: application,
    }


def configuration_value(field: FieldSpec) -> tuple[str, int]:
    if field.kind == "boolean":
        return "YES", 0
    if field.kind == "integer":
        return str(max(0, field.minimum)), 1
    if field.kind == "select":
        return field.choices[0][1], 3
    if field.kind == "color":
        return "0,0,0", 4
    return "", 2
