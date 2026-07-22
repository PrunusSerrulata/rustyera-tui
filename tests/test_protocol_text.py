import queue

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
    assert enum_text(5, RUNTIME_PHASES, "RuntimePhase") == "WaitingInput（等待输入）"
    assert (
        enum_list_text([3], SNAPSHOT_INELIGIBLE_REASONS, "SnapshotIneligibleReason")
        == "SnapshotStateUnavailable（当前快照状态不可用）"
    )
    assert (
        variant_enum_text([2, []], DEBUG_STOP_REASONS, "StopReason")
        == "StepCompleted（单步完成）"
    )
    assert enum_text(4, ERA_STATUSES, "EraStatus") == "AbiMismatch（ABI 不匹配）"


def test_unknown_wire_enums_remain_readable_without_numeric_only_output() -> None:
    assert enum_text(99, RUNTIME_PHASES, "RuntimePhase") == (
        "UnknownRuntimePhase（未知值 99）"
    )


def test_snapshot_rejection_event_uses_reason_names() -> None:
    client = RuntimeClient.__new__(RuntimeClient)
    client.events = queue.Queue()
    client.pending_export = (None, bytearray(), None)

    client._handle_export_ready({1: [1, [[3]]]})

    event = client.events.get_nowait()
    assert event.kind == "error"
    assert event.value == (
        "当前状态不能生成快照：SnapshotStateUnavailable（当前快照状态不可用）"
    )
    assert client.pending_export is None
