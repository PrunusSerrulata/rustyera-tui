"""Single source for the production ClientHello and captured JSON identity."""

from __future__ import annotations

from typing import Any, Protocol

from .project import DEFAULT_MAXIMUM_ENVELOPE_BYTES, DEFAULT_MAXIMUM_PAYLOAD_BYTES
from .wire import RUNTIME_VERSION, version_range

_FEATURES = (
    (0, "project_reload"),
    (1, "traditional_save"),
    (2, "vm_snapshot"),
    (3, "timed_input"),
    (4, "rich_text"),
    (10, "state_resynchronization"),
    (11, "storage"),
    (12, "input_undo"),
    (13, "project_analysis"),
    (14, "key_macros"),
)
_INPUT_MODALITIES = ((0, "keyboard"), (1, "mouse"))
_SERVICES = (
    (9, "entropy", "random_seed"),
    (8, "clock", "local_date_time"),
    (7, "input_state", "device_pump"),
    (1, "image", "image_metadata"),
    (10, "presentation_query", "get_display_line"),
    (10, "presentation_query", "html_get_printed_str"),
    (10, "presentation_query", "serialize_physical_history"),
    (0, "font_metrics", "gget_text_size"),
    (11, "sql", "rustyera.sql"),
)
_ENVIRONMENT = ("input.timed_viewport", "input.device_pump")


class _WireLimits(Protocol):
    def requested_wire_limits(self) -> tuple[int, int]: ...


def build_client_hello(bundle: _WireLimits | None) -> dict[int, Any]:
    maximum_envelope_bytes, maximum_payload_bytes = (
        bundle.requested_wire_limits()
        if bundle is not None
        else (DEFAULT_MAXIMUM_ENVELOPE_BYTES, DEFAULT_MAXIMUM_PAYLOAD_BYTES)
    )
    services = [{0: kind, 1: operation, 2: version_range(1, 0)} for kind, _, operation in _SERVICES]
    return {
        0: version_range(*RUNTIME_VERSION),
        1: "rustyera-textual-tui",
        2: [value for value, _ in _FEATURES],
        3: {
            0: maximum_envelope_bytes,
            1: maximum_payload_bytes,
            2: 128,
            3: 4096,
            4: 1_000_000,
            5: 1024 * 1024 * 1024,
            6: 64 * 1024 * 1024,
        },
        4: {
            0: [value for value, _ in _INPUT_MODALITIES],
            1: True,
            2: True,
            3: False,
            4: False,
            5: False,
            6: True,
            7: True,
            8: True,
            9: [],
            10: services,
            11: {0: True, 1: True, 2: True, 3: True},
            12: [{0: name, 1: version_range(1, 0)} for name in _ENVIRONMENT],
        },
        5: ["zh-CN", "ja", "en"],
        6: 1,
    }


def captured_client_identity() -> dict[str, Any]:
    exact = {
        "minimum": {"major": 1, "minor": 0},
        "maximum": {"major": 1, "minor": 0},
    }
    return {
        "features": [name for _, name in _FEATURES],
        "capabilities": {
            "environment": [{"name": name, "versions": exact} for name in _ENVIRONMENT],
            "input_modalities": [name for _, name in _INPUT_MODALITIES],
            "rich_text": True,
            "html": True,
            "graphics": False,
            "audio": False,
            "video": False,
            "font_metrics": True,
            "column_cells": True,
            "separators": True,
            "available_fonts": [],
            "services": [
                {"kind": name, "operation": operation, "versions": exact}
                for _, name, operation in _SERVICES
            ],
            "storage": {
                "revisions": True,
                "atomic_replace": True,
                "missing_precondition": True,
                "delete": True,
            },
        },
    }
