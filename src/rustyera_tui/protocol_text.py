"""Human-readable names for enum values carried by the public wire protocols."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


RUNTIME_PHASES = {
    0: "Negotiating",
    1: "LoadingProject",
    2: "Ready",
    3: "Starting",
    4: "Running",
    5: "WaitingInput",
    6: "WaitingExternal",
    7: "DebugPaused",
    8: "Reloading",
    9: "Stopping",
    10: "Stopped",
    11: "Faulted",
    12: "AnalyzingProject",
}

SNAPSHOT_INELIGIBLE_REASONS = {
    0: "StableWaitRequired",
    1: "ExternalOperationPending",
    2: "VmSnapshotUnavailable",
    3: "SnapshotStateUnavailable",
}

DEBUG_STOP_REASONS = {
    0: "PauseRequested",
    1: "Breakpoint",
    2: "StepCompleted",
    3: "HostWait",
    4: "FiberCompleted",
    5: "Fault",
    6: "Reload",
}

FAULT_CODES = {
    0: "InvalidState",
    1: "InvalidMessage",
    2: "ProjectLoad",
    3: "VmFault",
    4: "ServiceFailure",
    5: "ResourceLimit",
    6: "Internal",
    7: "UnsupportedRuntimeFeature",
}

COMMAND_ERROR_CODES = {
    0: "InvalidState",
    1: "InvalidValue",
    2: "StaleRequest",
    3: "VersionMismatch",
    4: "PermissionDenied",
    5: "FeatureUnavailable",
    6: "ResourceLimit",
}

DIAGNOSTIC_SEVERITIES = {
    0: "Debug",
    1: "Info",
    2: "Warning",
    3: "Error",
}

SERVICE_KINDS = {
    0: "FontMetrics",
    1: "Image",
    2: "Canvas",
    3: "Audio",
    4: "Network",
    5: "OpenUrl",
    6: "Extension",
    7: "InputState",
    8: "Clock",
    9: "Entropy",
    10: "PresentationQuery",
}

ERA_STATUSES = {
    0: "Ok",
    1: "Empty",
    2: "Busy",
    3: "InvalidArgument",
    4: "AbiMismatch",
    5: "InvalidHandle",
    6: "ResourceLimit",
    7: "InternalError",
}


def enum_text(value: Any, names: Mapping[int, str], enum_name: str) -> str:
    """Return a stable textual enum name while keeping unknown future values readable."""

    if isinstance(value, int):
        return names.get(value, f"Unknown{enum_name}[{value}]")
    return f"Invalid{enum_name}[{value!r}]"


def enum_list_text(values: Any, names: Mapping[int, str], enum_name: str) -> str:
    """Format a wire list of index-only enum values without leaking numeric-only output."""

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return enum_text(values, names, enum_name)
    return "、".join(enum_text(value, names, enum_name) for value in values)


def variant_enum_text(value: Any, names: Mapping[int, str], enum_name: str) -> str:
    """Format minicbor's ``[tag, fields]`` enum representation."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and value:
        return enum_text(value[0], names, enum_name)
    return enum_text(value, names, enum_name)
