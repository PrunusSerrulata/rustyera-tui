"""Pure geometry and color projection helpers for terminal HTML layout."""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, cast

from .presentation_helpers import packed_color_hex as _packed_color_hex
from .terminal_canvas import MAX_TERMINAL_COLUMNS, MAX_TERMINAL_ROWS
from .wire import unwrap_variant

TERMINAL_CELL_WIDTH_PX = 8
TERMINAL_CELL_HEIGHT_PX = 16


class _LengthDomain(Enum):
    HTML = auto()
    PRESENTATION = auto()


def _project_length(
    value: Any,
    *,
    horizontal: bool,
    domain: _LengthDomain,
    extent: bool = False,
) -> int | None:
    if value is None:
        return None
    try:
        unit, fields = unwrap_variant(value)
        raw_value = fields[0]
        raw = raw_value[0] if isinstance(raw_value, list) else raw_value
        raw = int(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if unit == 0:
        divisor = TERMINAL_CELL_WIDTH_PX if horizontal else TERMINAL_CELL_HEIGHT_PX
        if domain is _LengthDomain.PRESENTATION:
            divisor *= 1_000
    elif unit == 1:
        divisor = 50 if horizontal else 100
    else:
        return None
    projected = _round_away_from_zero(raw, divisor)
    if extent and raw != 0:
        projected = max(1, abs(projected))
    limit = MAX_TERMINAL_COLUMNS if horizontal else MAX_TERMINAL_ROWS
    return projected if -limit <= projected <= limit else None


def _project_box_sides(values: Any) -> tuple[int, int, int, int]:
    if not isinstance(values, list) or len(values) != 4:
        return 0, 0, 0, 0
    axes = (False, True, False, True)
    projected = [
        _project_length(
            value,
            horizontal=horizontal,
            domain=_LengthDomain.HTML,
        )
        for value, horizontal in zip(values, axes, strict=True)
    ]
    return cast(
        tuple[int, int, int, int],
        tuple(max(0, value or 0) for value in projected),
    )


def _project_border_sides(values: Any) -> tuple[int, int, int, int]:
    if not isinstance(values, list) or len(values) != 4:
        return 0, 0, 0, 0
    top, right, bottom, left = (_raw_length_positive(value) for value in values)
    return int(top), int(right), int(bottom), int(left)


def _border_colors(
    borders: tuple[int, int, int, int],
    colors: Any,
    fallback: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    packed = colors if isinstance(colors, list) and len(colors) == 4 else None
    result: list[str | None] = []
    for index, present in enumerate(borders):
        if not present:
            result.append(None)
        elif packed is not None and isinstance(packed[index], int):
            result.append(_packed_color_hex(packed[index]))
        else:
            result.append(fallback)
    return result[0], result[1], result[2], result[3]


def _raw_length_positive(value: Any) -> bool:
    try:
        _tag, fields = unwrap_variant(value)
        raw_value = fields[0]
        raw = raw_value[0] if isinstance(raw_value, list) else raw_value
        return int(raw) > 0
    except (TypeError, ValueError, IndexError):
        return False


def _bounded_depth(value: Any) -> int | None:
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return None
    return depth if -32_768 <= depth <= 32_767 else None


def _round_away_from_zero(value: int, divisor: int) -> int:
    magnitude = (abs(value) + divisor // 2) // divisor
    return magnitude if value >= 0 else -magnitude


def _shape_color(value: Any) -> str | None:
    if isinstance(value, dict):
        return _color_hex(value)
    if isinstance(value, int):
        return _packed_color_hex(value)
    return None


def _color_hex(color: dict[int, int]) -> str:
    return f"#{color.get(0, 0):02x}{color.get(1, 0):02x}{color.get(2, 0):02x}"
