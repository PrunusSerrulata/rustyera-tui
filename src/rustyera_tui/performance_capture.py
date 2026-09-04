"""Audit-only capture of transient runtime envelopes consumed inside a worker pump."""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any


def _stable(value: Any) -> Any:
    if isinstance(value, bytes):
        return list(value)
    if isinstance(value, dict):
        return {str(key): _stable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    return value


def _version(value: Any) -> Any:
    if not isinstance(value, dict):
        return _stable(value)
    minimum = value.get(0, {})
    maximum = value.get(1, {})
    return {
        "minimum": {"major": minimum.get(0), "minor": minimum.get(1)},
        "maximum": {"major": maximum.get(0), "minor": maximum.get(1)},
    }


def _storage_operation(value: Any) -> Any:
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], list):
        return _stable(value)
    tag, fields = value
    names = {0: "read", 1: "write", 2: "list", 3: "delete", 4: "stat", 5: "read_range"}
    operation: dict[str, Any] = {"type": names.get(tag, f"unknown_{tag}")}
    if tag == 1 and len(fields) == 3:
        operation.update(data=_stable(fields[0]), atomic_replace=fields[1], precondition=_stable(fields[2]))
    elif tag == 2 and len(fields) == 2:
        operation.update(pattern=fields[0], recursive=fields[1])
    elif tag == 3 and len(fields) == 1:
        operation["precondition"] = _stable(fields[0])
    elif tag == 5 and len(fields) == 3:
        operation.update(offset=fields[0], maximum_bytes=fields[1], change_token=fields[2])
    return operation


def _storage_request(value: Any) -> Any:
    if not isinstance(value, dict):
        return _stable(value)
    namespaces = {0: "project", 1: "save", 2: "global_save", 3: "data", 4: "log", 5: "resource"}
    return {
        "namespace": namespaces.get(value.get(1), f"unknown_{value.get(1)}"),
        "relativePath": value.get(2),
        "operation": _storage_operation(value.get(3)),
    }


class AuditCaptureState:
    """Retain only the current checkpoint interval and remove volatile request IDs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._services: list[dict[str, Any]] = []
        self._storage: list[Any] = []
        self._other_runtime_tags: list[int] = []

    def observe_runtime(self, tag: int, value: Any) -> None:
        with self._lock:
            if tag == 52 and isinstance(value, dict):
                service_kinds = {
                    0: "font_metrics",
                    1: "image",
                    2: "canvas",
                    3: "audio",
                    4: "network",
                    5: "open_url",
                    6: "extension",
                    7: "input_state",
                    8: "clock",
                    9: "entropy",
                    10: "presentation_query",
                    11: "sql",
                }
                self._services.append(
                    {
                        "kind": service_kinds.get(value.get(1), f"unknown_{value.get(1)}"),
                        "operation": value.get(2),
                        "operationVersion": _version(value.get(3)),
                        "payload": _stable(value.get(4)),
                    }
                )
            elif tag == 50:
                self._storage.append(_storage_request(value))
            else:
                self._other_runtime_tags.append(tag)

    def take_interval(self) -> dict[str, Any]:
        with self._lock:
            captured = {
                "services": deepcopy(self._services),
                "storage": deepcopy(self._storage),
                "otherOutboundTags": list(self._other_runtime_tags),
            }
            self._services.clear()
            self._storage.clear()
            self._other_runtime_tags.clear()
            return captured
