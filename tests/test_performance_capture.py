from __future__ import annotations

from rustyera_tui.performance_capture import AuditCaptureState


def test_transient_capture_removes_request_ids_and_deadlines() -> None:
    capture = AuditCaptureState()
    capture.observe_runtime(
        52,
        {
            0: 99,
            1: 11,
            2: "rustyera.sql",
            3: {0: {0: 1, 1: 0}, 1: {0: 1, 1: 0}},
            4: b"sql",
            5: 123456,
        },
    )
    capture.observe_runtime(50, {0: 88, 1: 1, 2: "slot.sav", 3: [0, []], 5: 654321})
    capture.observe_runtime(41, {0: 1})

    interval = capture.take_interval()

    assert interval["services"] == [
        {
            "kind": "sql",
            "operation": "rustyera.sql",
            "operationVersion": {
                "minimum": {"major": 1, "minor": 0},
                "maximum": {"major": 1, "minor": 0},
            },
            "payload": [115, 113, 108],
        }
    ]
    assert interval["storage"] == [
        {"namespace": "save", "relativePath": "slot.sav", "operation": {"type": "read"}}
    ]
    assert interval["otherOutboundTags"] == [41]
    assert capture.take_interval() == {
        "services": [],
        "storage": [],
        "otherOutboundTags": [],
    }
