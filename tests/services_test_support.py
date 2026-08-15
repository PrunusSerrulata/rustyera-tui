from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import blake3
import pytest

import rustyera_tui.runtime as runtime_module
from rustyera_tui.configuration import ConfigurationChange, ConfigurationSnapshot
from rustyera_tui.project import FILE_RESOURCE, ProjectBundle, ProjectFile, StorageBackend
from rustyera_tui.presentation import ServicePresentationModel
from rustyera_tui.runtime import (
    AtomicExportStream,
    DiagnosisProgress,
    FrontendEvent,
    FullProjectExport,
    PendingStateImport,
    PendingConfigurationPrepare,
    PendingGameInput,
    RuntimeClient,
    RuntimeFailure,
)
from rustyera_tui.runtime_export import DiagnosisExport, ExportStage
from rustyera_tui.wire import decode, encode, unwrap_variant, variant

__all__ = [
    "Any",
    "AtomicExportStream",
    "ConfigurationChange",
    "ConfigurationSnapshot",
    "DiagnosisExport",
    "DiagnosisProgress",
    "ExportStage",
    "FILE_RESOURCE",
    "FrontendEvent",
    "FullProjectExport",
    "Path",
    "PendingConfigurationPrepare",
    "PendingGameInput",
    "PendingStateImport",
    "ProjectBundle",
    "ProjectFile",
    "RuntimeClient",
    "RuntimeFailure",
    "SimpleNamespace",
    "StorageBackend",
    "blake3",
    "client_with_capture",
    "debug_client_with_capture",
    "encode",
    "next_event_of_kind",
    "pytest",
    "ready_payload",
    "runtime_module",
    "unwrap_variant",
    "variant",
]


def client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any]]]:
    client = object.__new__(RuntimeClient)
    client.presentation = ServicePresentationModel(
        revision=7,
        lines=[{0: 1, 5: [[0, ["你好 RustyEra", None, None]]]}],
    )
    client.events = queue.Queue()
    client.session = {0: 1, 1: 2}
    client.phase = 6
    client.active_wait = None
    client._projection_messages = set()
    client._input_messages = {}
    client.pending_cache_export_message = None
    client.pending_export_message = None
    client.pending_export_kind = None
    client.pending_export = None
    client.pending_cache_stream = None
    client.full_project_export = None
    client.cache_preparation_started = False
    client.cache_refresh_pending = False
    client.cache_refresh_after_ns = 0
    client.pending_diagnosis = None
    client.pending_start_after_configuration = None
    client.pending_restore = None
    client.pending_import = None
    client.new_game_seed = None
    client.configuration_profile_supported = True
    client.abi = SimpleNamespace(
        supports_project_configuration_updates=True,
        prepare_project_configuration_update=lambda *_args: (0, b""),
        project_file_manifest=lambda _bytes: {0: 1, 1: []},
    )
    client.single_step_enabled = False
    captured: list[tuple[int, Any]] = []
    client.send_runtime = (  # type: ignore[method-assign]
        lambda tag, value, **_kwargs: captured.append((tag, value)) or 1
    )
    return client, captured


def next_event_of_kind(client: RuntimeClient, kind: str) -> FrontendEvent:
    while True:
        event = client.events.get_nowait()
        if event.kind == kind:
            return event


def ready_payload(captured: list[tuple[int, Any]]) -> dict[int, Any]:
    assert captured[0][0] == 53
    result_tag, result_fields = unwrap_variant(captured[0][1][1])
    assert result_tag == 0
    return decode(result_fields[0])


def debug_client_with_capture() -> tuple[RuntimeClient, list[tuple[int, Any, str]]]:
    client = object.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.phase = 4
    client.active_wait = None
    client.debug_requested = True
    client.debug_grant = {1: {0: {0: 7, 1: 9}}}
    client.stop_token = None
    client.selected_fiber = None
    client.pending_debug_actions = []
    client.debug_pending_by_message = {}
    client.single_step_enabled = False
    client.debug_step_in_flight = False
    client.debug_disable_pending = False
    client.transient_pause_owner = None
    client.transient_close_pending = None
    captured: list[tuple[int, Any, str]] = []
    client.send_debug = (  # type: ignore[method-assign]
        lambda tag, value, pending="": captured.append((tag, value, pending)) or 1
    )
    return client, captured
