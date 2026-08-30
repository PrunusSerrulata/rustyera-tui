"""Shared, Textual-independent helpers for terminal presentation projection."""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len

from .presentation_types import CellWidthIntent, DisplaySegment
from .wire import unwrap_variant


def semantic_field(semantic: Any, index: int, default: Any = None) -> Any:
    if semantic is None:
        return default
    try:
        _tag, fields = unwrap_variant(semantic)
    except (TypeError, ValueError):
        return default
    return fields[index] if index < len(fields) else default


def packed_color_hex(value: int) -> str:
    return f"#{(value >> 16) & 0xFF:02x}{(value >> 8) & 0xFF:02x}{value & 0xFF:02x}"


def cell_width(width: Any) -> tuple[CellWidthIntent, int]:
    """Decode one protocol-45 cell width intent at the untrusted wire boundary."""

    tag, fields = unwrap_variant(width)
    if len(fields) != 1 or not isinstance(fields[0], int) or isinstance(fields[0], bool):
        raise ValueError("column cell width must contain one unsigned integer")
    value = int(fields[0])
    if not 0 <= value <= 0xFFFF_FFFF:
        raise ValueError("column cell width must fit u32")
    try:
        return CellWidthIntent(tag), value
    except ValueError as error:
        raise ValueError(f"unsupported column cell width intent {tag}") from error


def segment_columns(segment: DisplaySegment) -> int:
    if segment.logical_columns is not None and "\n" not in segment.text:
        return segment.logical_columns
    return cell_len(segment.text)
