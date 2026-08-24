"""Shared imports, constants, and state records for the runtime client facade."""

# This private dependency barrel deliberately re-exports the names consumed by the
# runtime mixins, keeping their compatibility facade free of duplicated imports.
# ruff: noqa: F401

from __future__ import annotations

import copy
import queue
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import blake3
from rich.cells import cell_len

from .abi import RuntimeAbi
from .configuration import (
    APPLICATION_HOT,
    ConfigurationChange,
    ConfigurationSnapshot,
    PreparedConfiguration,
)
from .diagnosis import diagnosis_project_name, write_diagnosis_archive
from .image_metadata import decode_image_metadata
from .input_policy import is_message_skip_wait, is_message_wait, message_wait_intent
from .log_model import LogLevel
from .presentation import (
    PresentationEventAccumulator,
    ServicePresentationModel,
    html_printed_str,
    plain_line,
)
from .project import ProjectBundle, StorageBackend
from .protocol_text import (
    COMMAND_ERROR_CODES,
    SERVICE_KINDS,
    SNAPSHOT_INELIGIBLE_REASONS,
    enum_list_text,
    enum_text,
)
from .runtime_export import (
    DIAGNOSIS_EXPORT_STAGES,
    RUNTIME_EXPORT_KIND,
    AtomicExportStream,
    DiagnosisExport,
    ExportStage,
    FullProjectExport,
    PendingStateImport,
    atomic_write,
)
from .runtime_support import (
    RuntimeFailure,
    format_project_diagnostic,
    log_event,
    runtime_log_level,
)
from .runtime_types import (
    DiagnosisProgress,
    DiagnosisProgressStage,
    FrontendCommand,
    FrontendEvent,
    GameInformation,
    PresentationBatch,
)
from .startup_telemetry import emit_startup_milestone as _emit_startup_milestone
from .wire import (
    CHANNEL_DEBUG,
    CHANNEL_RUNTIME,
    DEBUG_VERSION,
    RUNTIME_VERSION,
    debug_message,
    decode,
    decode_envelope,
    encode,
    encode_envelope,
    message_value,
    runtime_message,
    unwrap_variant,
    variant,
    version_range,
)


def emit_startup_milestone(*args: Any, **kwargs: Any) -> None:
    """Resolve the compatibility facade hook at call time for test and host instrumentation."""

    import sys

    facade = sys.modules.get("rustyera_tui.runtime")
    hook = getattr(facade, "emit_startup_milestone", _emit_startup_milestone)
    if hook is emit_startup_milestone:
        hook = _emit_startup_milestone
    hook(*args, **kwargs)


CORE_STARTUP_PHASES = {
    1: "normalize_ms",
    2: "csv_ms",
    3: "parse_ms",
    4: "analyze_ms",
    5: "compile_ms",
    6: "validate_ms",
    7: "compile_finalize_ms",
    8: "prepare_ms",
    10: "cache_parse_ms",
    11: "cache_decode_ms",
    12: "cache_validate_ms",
}

DIAGNOSIS_PROGRESS_STAGE_BY_EXPORT: dict[ExportStage, DiagnosisProgressStage] = {
    ExportStage.DIAGNOSIS_REPLAY: "input_replay",
    ExportStage.DIAGNOSIS_SNAPSHOT: "vm_snapshot",
    ExportStage.DIAGNOSIS_PROJECT: "project_transfer",
}

COMPILED_CACHE_PERSIST_DELAY_NS = 10_000_000_000
COMPILED_CACHE_RETRY_NS = 250_000_000
STATE_IMPORT_CHUNK_BYTES = 16 * 1024 * 1024
FULL_PROJECT_MANIFEST_CHUNK_BYTES = 4 * 1024 * 1024
STATE_EXPORT_CHUNK_BYTES = 16 * 1024 * 1024


def _debug_action_owner(action: str) -> str | None:
    if action.startswith("console_"):
        return "console"
    if action in {"variables", "read_variable"}:
        return "variables"
    if action in {"fibers", "call_stack"}:
        return "stack"
    return None


@dataclass(slots=True)
class PendingGameInput:
    wait: dict[int, Any]
    intent: list[Any]
    message_skip: bool
    stale_retries: int = 0


@dataclass(frozen=True, slots=True)
class PendingConfigurationPrepare:
    message_id: int
    project_revision: int
    source_digest: bytes
    restart: bool
    session_only: bool = False
    automatic: bool = False


@dataclass(frozen=True, slots=True)
class PreparedConfigurationCommit:
    prepared: PreparedConfiguration
    candidate: ProjectBundle | None


@dataclass(frozen=True, slots=True)
class PreparedConfigurationAbort:
    error: str


@dataclass(frozen=True, slots=True)
class PendingConfigurationFinalize:
    finalize_message_id: int
    preparation_message_id: int
    restart: bool
    outcome: PreparedConfigurationCommit | PreparedConfigurationAbort
    automatic: bool = False
