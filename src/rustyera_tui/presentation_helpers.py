"""Shared, Textual-independent helpers for terminal presentation projection."""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len

from .presentation_types import DisplaySegment
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


def segment_columns(segment: DisplaySegment) -> int:
    if segment.logical_columns is not None and "\n" not in segment.text:
        return segment.logical_columns
    return cell_len(segment.text)
