"""Private plain-text and HTML service projection for normalized presentation runs."""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len

from .presentation_helpers import semantic_field as _semantic_field
from .presentation_types import DEFAULT_VIEWPORT_COLUMNS
from .wire import unwrap_variant


def plain_line(line: dict[int, Any]) -> str:
    return "".join(plain_run(run) for run in line.get(5, []))


def html_printed_str(lines: list[dict[int, Any]], line_number: int) -> str:
    """Serialize one newest-first logical line using Emuera's HTML wrapper."""
    if line_number < 0:
        return ""
    count = 0
    selected: list[dict[int, Any]] = []
    for line in reversed(lines):
        if count == line_number:
            selected.insert(0, line)
        if line.get(2, True):
            count += 1
        if count > line_number:
            break
    if not selected:
        return ""
    alignment = {0: "left", 1: "center", 2: "right"}.get(selected[0].get(4, 0), "left")
    body = "<br>".join(_escape_html(plain_line(line)) for line in selected)
    return f"<p align='{alignment}'><nobr>{body}</nobr></p>"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace(">", "&gt;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def plain_run(run: list[Any]) -> str:
    tag, fields = unwrap_variant(run)
    if tag in (0, 8):
        return fields[0]
    if tag == 1:
        return "".join(plain_run(child) for child in fields[0])
    if tag == 2:
        return "".join(_plain_html_node(node) for node in fields[0].get(0, []))
    if tag == 3:
        return fields[1] or "[图片]"
    if tag == 4:
        return "[图形]"
    if tag == 5:
        content, alignment, preferred_columns = fields
        text = "".join(plain_run(child) for child in content)
        width = sum(_plain_run_columns(child) for child in content)
        padding = " " * max(0, preferred_columns - width)
        return f"{padding}{text}" if alignment == 1 else f"{text}{padding}"
    if tag == 6:
        pattern = fields[0] or "-"
        return pattern * max(1, DEFAULT_VIEWPORT_COLUMNS // len(pattern))
    if tag == 7:
        width_tag, width_fields = unwrap_variant(fields[0])
        raw = width_fields[0]
        if isinstance(raw, list):
            raw = raw[0]
        return " " * max(1, round(raw / (1000 if width_tag == 0 else 100)))
    return f"[未支持的显示片段 {tag}]"


def _plain_run_columns(run: list[Any]) -> int:
    tag, fields = unwrap_variant(run)
    if tag == 8:
        return max(0, int(fields[3]))
    if tag == 0:
        return cell_len(fields[0])
    if tag in (1, 5):
        return sum(_plain_run_columns(child) for child in fields[0])
    return cell_len(plain_run(run))


def _plain_html_node(node: list[Any]) -> str:
    try:
        tag, fields = unwrap_variant(node)
    except (TypeError, ValueError):
        return ""
    if tag == 0 and fields:
        return str(fields[0])
    if tag != 1 or len(fields) < 3 or not isinstance(fields[2], list):
        return ""
    kind = fields[0]
    if kind == 13:
        return "\n"
    if kind == 10:
        return ""
    if kind == 11:
        semantic = fields[6] if len(fields) > 6 else None
        shape_kind = str(_semantic_field(semantic, 0, ""))
        if shape_kind.lower() == "space":
            parameters = _semantic_field(semantic, 1, [])
            if isinstance(parameters, list) and parameters:
                try:
                    width_tag, width_fields = unwrap_variant(parameters[0])
                    raw = width_fields[0]
                    if isinstance(raw, list):
                        raw = raw[0]
                    return " " * max(1, round(int(raw) / (1 if width_tag == 0 else 50)))
                except (IndexError, TypeError, ValueError):
                    pass
        return "[图形]"
    return "".join(_plain_html_node(child) for child in fields[2])
