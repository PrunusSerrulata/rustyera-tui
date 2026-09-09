from __future__ import annotations

import json

import pytest

from rustyera_tui.client_hello import captured_client_identity
from rustyera_tui.performance_audit import freeze_capture
from rustyera_tui.performance_trace import (
    PerformanceTrace,
    PerformanceTraceError,
    canonical_digest,
    capture_template,
)


def normalized_checkpoint() -> dict[str, object]:
    return {
        "phase": "waiting_input",
        "wait": {
            "kind": "integer_value",
            "stability": 0,
            "systemInput": False,
            "timed": False,
        },
        "lines": [],
        "resources": [],
        "scene": {"revision": 0, "layers": []},
        "variables": {"FLAG:0": 1},
        "services": [],
        "storage": [],
        "otherOutboundTags": [],
    }


def valid_trace() -> dict[str, object]:
    normalized = normalized_checkpoint()
    trace: dict[str, object] = {
        "schemaVersion": 1,
        "scenario": "fixture",
        "projectDigest": "1" * 64,
        "seed": 1,
        "client": captured_client_identity(),
        "setupMessages": [],
        "steps": [
            {
                "id": "ready",
                "checkpoint": "ready-input",
                "expect": {
                    "phase": "waiting_input",
                    "waitKind": "integer_value",
                    "textContains": [],
                    "outboundTags": [],
                    "services": [],
                    "storage": [],
                    "variables": {"FLAG:0": 1},
                    "stateSignature": canonical_digest(normalized),
                },
                "action": {"kind": "none"},
            }
        ],
    }
    trace["traceDigest"] = canonical_digest(trace, omit="traceDigest")
    return trace


def test_capture_template_is_deliberately_not_replayable(tmp_path) -> None:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(capture_template(scenario="fixture")), encoding="utf-8")

    with pytest.raises(PerformanceTraceError, match="capture-required"):
        PerformanceTrace.load(path, scenario="fixture")


def test_trace_recomputes_digest(tmp_path) -> None:
    trace = valid_trace()
    trace["scenario"] = "changed"
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(PerformanceTraceError, match="trace digest mismatch"):
        PerformanceTrace.load(path, scenario="changed")


def test_trace_rejects_nonterminal_action(tmp_path) -> None:
    trace = valid_trace()
    trace["steps"][0]["action"] = {  # type: ignore[index]
        "kind": "input",
        "intent": {"type": "commit_text", "value": "0"},
    }
    trace["traceDigest"] = canonical_digest(trace, omit="traceDigest")
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(PerformanceTraceError, match="final"):
        PerformanceTrace.load(path, scenario="fixture")


def test_freeze_removes_candidate_only_fields_without_overwriting(tmp_path) -> None:
    candidate = valid_trace()
    candidate.pop("traceDigest")
    candidate["profile"] = "emuera.skia.snake"
    candidate["captureRequired"] = False
    candidate["steps"][0]["normalized"] = normalized_checkpoint()  # type: ignore[index]
    candidate["steps"][0].pop("expect")  # type: ignore[index]
    source = tmp_path / "candidate.json"
    target = tmp_path / "trace.json"
    source.write_text(json.dumps(candidate), encoding="utf-8")

    freeze_capture(source, target)

    frozen = json.loads(target.read_text(encoding="utf-8"))
    assert set(frozen) == {
        "schemaVersion",
        "traceDigest",
        "scenario",
        "projectDigest",
        "seed",
        "client",
        "setupMessages",
        "steps",
    }
    assert "normalized" not in frozen["steps"][0]
    assert frozen["steps"][0]["expect"]["phase"] == "waiting_input"
    with pytest.raises(FileExistsError):
        freeze_capture(source, target)
