import queue
from pathlib import Path

import blake3
from rustyera_tui.protocol_text import (
    DEBUG_STOP_REASONS,
    ERA_STATUSES,
    RUNTIME_PHASES,
    SNAPSHOT_INELIGIBLE_REASONS,
    enum_list_text,
    enum_text,
    variant_enum_text,
)
from rustyera_tui.runtime import RuntimeClient


def test_known_wire_enums_use_textual_protocol_names() -> None:
    assert enum_text(5, RUNTIME_PHASES, "RuntimePhase") == "WaitingInput"
    assert (
        enum_list_text([3], SNAPSHOT_INELIGIBLE_REASONS, "SnapshotIneligibleReason")
        == "SnapshotStateUnavailable"
    )
    assert (
        variant_enum_text([2, []], DEBUG_STOP_REASONS, "StopReason")
        == "StepCompleted"
    )
    assert enum_text(4, ERA_STATUSES, "EraStatus") == "AbiMismatch"


def test_unknown_wire_enums_remain_readable_without_numeric_only_output() -> None:
    assert enum_text(99, RUNTIME_PHASES, "RuntimePhase") == "UnknownRuntimePhase[99]"


def test_snapshot_rejection_event_uses_reason_names() -> None:
    client = RuntimeClient.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.pending_export = (None, bytearray(), None)

    client._handle_export_ready({1: [1, [[3]]]})

    event = client.events.get_nowait()
    assert event.kind == "error"
    assert event.value == (
        "当前状态不能生成快照：SnapshotStateUnavailable"
    )
    finished = client.events.get_nowait()
    assert finished.kind == "snapshot_export_finished"
    assert finished.value is False
    assert client.pending_export is None


def test_snapshot_export_emits_a_dedicated_completion_event(tmp_path: Path) -> None:
    client = RuntimeClient.__new__(RuntimeClient)
    client.events = queue.Queue()
    data = b"snapshot"
    path = tmp_path / "runtime.snapshot"
    descriptor = {0: 7, 2: len(data), 3: blake3.blake3(data).digest()}
    client.pending_export = (path, bytearray(), descriptor)

    client._handle_export_chunk({0: 7, 1: 0, 2: data, 3: True})

    assert path.read_bytes() == data
    finished = client.events.get_nowait()
    assert finished.kind == "snapshot_export_finished"
    assert finished.value is True
    assert client.events.get_nowait().kind == "status"
