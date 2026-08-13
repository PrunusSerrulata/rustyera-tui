"""Pure control-value conversion for the preferences dialog."""

from __future__ import annotations

from typing import Any

from textual.widgets import Checkbox, Input, Select

from .color_picker import ColorField
from .preferences_schema import FieldSpec


def _field_id(code: str) -> str:
    return f"preference-{code.lower().replace('_', '-')}"


def control_value(query: Any, field: FieldSpec) -> str:
    control = query(f"#{_field_id(field.code)}")
    if isinstance(control, Checkbox):
        return "YES" if control.value else "NO"
    if isinstance(control, Select):
        return str(control.value)
    if isinstance(control, ColorField):
        return control.value
    if isinstance(control, Input):
        if field.kind == "integer":
            value = int(control.value)
            if not field.minimum <= value <= field.maximum:
                raise ValueError(f"必须在 {field.minimum} 到 {field.maximum} 之间")
            return str(value)
        return control.value
    raise TypeError(f"unknown settings control for {field.code}")


def set_control_value(query: Any, field: FieldSpec, value: str) -> None:
    control = query(f"#{_field_id(field.code)}")
    if isinstance(control, Checkbox):
        control.value = value == "YES"
    elif isinstance(control, ColorField | Select | Input):
        control.value = value
