from __future__ import annotations

import json
import ctypes
import queue
import shutil
import tarfile
import time
from pathlib import Path
from typing import Callable

import pytest
import zstandard

from rustyera_tui.abi import (
    DEFAULT_MAXIMUM_VM_INSTRUCTIONS,
    STATUS_INVALID_ARGUMENT,
    AbiError,
    EraCallHeader,
    RuntimeAbi,
    _borrowed_bytes,
    _header,
    discover_library,
)
from rustyera_tui.project import ProjectBundle, StorageBackend
from rustyera_tui.runtime import (
    DiagnosisProgress,
    FrontendCommand,
    FrontendEvent,
    PresentationBatch,
    RuntimeClient,
    RuntimeWorker,
)
from rustyera_tui.runtime_types import GameInformation

__all__ = [
    "AbiError",
    "DEFAULT_MAXIMUM_VM_INSTRUCTIONS",
    "DiagnosisProgress",
    "EraCallHeader",
    "FrontendCommand",
    "FrontendEvent",
    "GameInformation",
    "Path",
    "PresentationBatch",
    "ProjectBundle",
    "RuntimeAbi",
    "RuntimeClient",
    "RUNTIME_LIBRARY",
    "RuntimeWorker",
    "STATUS_INVALID_ARGUMENT",
    "StorageBackend",
    "_borrowed_bytes",
    "_header",
    "ctypes",
    "discover_library",
    "json",
    "pytest",
    "queue",
    "shutil",
    "tarfile",
    "wait_for",
    "wait_for_input",
    "wait_for_path",
    "zstandard",
]

try:
    RUNTIME_LIBRARY = discover_library(resource_directory=Path(__file__).parents[1])
except AbiError:
    RUNTIME_LIBRARY = None


def wait_for(
    worker: RuntimeWorker,
    predicate: Callable[[FrontendEvent], bool],
    timeout: float = 15,
) -> FrontendEvent:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = worker.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if predicate(event):
            return event
        if event.kind in ("error", "runtime_error", "runtime_fault"):
            raise AssertionError(event.value)
    raise AssertionError("timed out waiting for runtime event")


def wait_for_input(worker: RuntimeWorker) -> dict[int, object]:
    event = wait_for(
        worker,
        lambda candidate: (
            candidate.kind == "presentation_batch" and candidate.value.active_wait is not None
        ),
    )
    return event.value.active_wait


def wait_for_path(worker: RuntimeWorker, path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    observed: list[FrontendEvent] = []
    while time.monotonic() < deadline:
        if path.is_file():
            return
        try:
            event = worker.events.get(timeout=0.02)
        except queue.Empty:
            continue
        observed.append(event)
        if event.kind in ("error", "runtime_error", "runtime_fault"):
            raise AssertionError(event.value)
    client = worker.client
    state = (
        None
        if client is None
        else {
            "pending": client.cache_refresh_pending,
            "ready": client.cache_ready,
            "after": client.pending_cache_after,
            "export_kind": client.pending_export_kind,
            "export_message": client.pending_cache_export_message,
        }
    )
    raise AssertionError(f"timed out waiting for {path}; state={state}; events={observed[-20:]}")
