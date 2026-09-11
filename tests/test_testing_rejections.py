from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from services_test_support import client_with_capture

from rustyera_tui.runtime import PendingStateImport
from rustyera_tui.testing import RustTestSession, Scenario, TestDriverError
from rustyera_tui.testing_rejections import check_compiled_cache_expectation, reject_snapshot


def rejection_session(tmp_path: Path, *, code: int = 3, context=None, change_wait=False):
    client, captured = client_with_capture()
    wait = {0: 9, 1: 2, 2: 0, 5: True, 11: {0: 3, 1: 9}}
    client.active_wait = wait
    client.phase = 5
    client.epoch = 3
    session = object.__new__(RustTestSession)
    session.model = SimpleNamespace(lines=[])
    session.previous_output = []
    session.statuses = []
    session.logs = []
    session.metrics = []
    session._last_wait = (9, wait[11])
    calls = []

    def send(operation, value=None):
        calls.append((operation, value))
        if operation == "restore_snapshot":
            client.pending_restore = (value, None, "snapshot")
            client.pending_import = PendingStateImport(
                kind=1,
                purpose="snapshot",
                total_bytes=3,
                path=value,
                begin_message_id=21,
                command_message_ids={21},
            )
            client._handle_command_rejection({0: code, 1: "profile mismatch", 4: context}, 21)
            if change_wait:
                client.active_wait = {**wait, 0: 10}

    session.worker = SimpleNamespace(
        events=client.events, client=client, send=send, is_alive=lambda: True
    )
    path = tmp_path / "old.snapshot"
    path.write_bytes(b"old")
    return session, path, calls, captured


def test_snapshot_rejection_observes_correlated_wire_fields_and_preserves_input(tmp_path):
    session, path, calls, _ = rejection_session(tmp_path)
    result = reject_snapshot(session, path, {"code": 3, "context": None}, time.monotonic() + 1)
    assert result["rejection"]["code"] == 3
    assert result["rejection"]["context"] is None
    assert result["rejection"]["correlation_id"] == 21
    assert result["wait_preserved"] is True
    assert result["observation"]["wait"]["id"] == 9
    assert session.worker.client.pending_import is None
    assert session.worker.client.pending_restore is None
    session.submit("7")
    assert calls == [("restore_snapshot", path), ("submit_text", "7")]


@pytest.mark.parametrize(
    "options", [{"code": 1}, {"context": {1: "unexpected"}}, {"change_wait": True}]
)
def test_snapshot_rejection_rejects_wrong_code_context_or_changed_wait(tmp_path, options):
    session, path, _, _ = rejection_session(tmp_path, **options)
    with pytest.raises(TestDriverError):
        reject_snapshot(session, path, {"code": 3, "context": None}, time.monotonic() + 1)


def test_fixed_snapshot_action_resolves_explicit_path_and_disallows_reference(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "old.snapshot").write_bytes(b"old")
    path = tmp_path / "scenario.json"
    raw = {
        "schema_version": 1,
        "project": "project",
        "seed": 1,
        "compiled_cache_expectation": "source_fallback",
        "inputs": [
            {
                "action": "reject_snapshot",
                "path": "old.snapshot",
                "expect_rejection": {"code": 3, "context": None},
            }
        ],
    }
    path.write_text(json.dumps(raw))
    scenario = Scenario.load(path)
    assert scenario.inputs[0]["path"] == str(tmp_path / "old.snapshot")
    assert scenario.compiled_cache_expectation == "source_fallback"
    raw["comparison"] = {"reference": True}
    path.write_text(json.dumps(raw))
    with pytest.raises(TestDriverError, match="frontend action"):
        Scenario.load(path)
    raw.pop("comparison")
    raw["inputs"][0]["expect_rejection"] = {"code": True, "context": None}
    path.write_text(json.dumps(raw))
    with pytest.raises(TestDriverError, match="protocol command error"):
        Scenario.load(path)


def test_cache_source_fallback_requires_correlated_structured_reports():
    diagnostic = {
        0: "runtime.compiled_cache_ignored",
        1: 3,
        2: "any localized message",
        3: None,
        4: {1: "compiled_cache", 2: None},
    }
    missed = {"correlation_id": 21, "report": {0: 7, 1: False, 2: [diagnostic], 3: True}}
    compiled = {"correlation_id": 22, "report": {0: 7, 1: True, 2: [], 3: False}}
    check_compiled_cache_expectation("source_fallback", True, [missed, compiled], [])
    check_compiled_cache_expectation("hit", True, [], ["runtime.compiled_cache_hit"])
    check_compiled_cache_expectation("hit", False, [], [])
    invalid = [
        [],
        [missed],
        [compiled],
        [{**missed, "correlation_id": None}, compiled],
        [{"correlation_id": 21, "report": {**missed["report"], 3: False}}, compiled],
        [{"correlation_id": 21, "report": {**missed["report"], 2: []}}, compiled],
        [missed, {"correlation_id": 22, "report": {**compiled["report"], 0: 8}}],
        [missed, {"correlation_id": 22, "report": {**compiled["report"], 1: False}}],
        [
            missed,
            compiled,
            {
                "correlation_id": 23,
                "report": {0: 7, 1: True, 2: [{0: "runtime.compiled_cache_hit"}], 3: False},
            },
        ],
    ]
    for reports in invalid:
        with pytest.raises(TestDriverError):
            check_compiled_cache_expectation(
                "source_fallback", True, reports, ["项目缓存未命中，正在读取项目源码…"]
            )
    with pytest.raises(TestDriverError):
        check_compiled_cache_expectation("source_fallback", False, [missed, compiled], [])


def test_snapshot_rejection_does_not_consume_an_unrelated_runtime_error(tmp_path):
    from rustyera_tui.runtime import FrontendEvent

    session, path, _, _ = rejection_session(tmp_path)
    session.worker.events.put(FrontendEvent("runtime_error", "unrelated failure"))
    with pytest.raises(TestDriverError, match="unrelated failure"):
        reject_snapshot(session, path, {"code": 3, "context": None}, time.monotonic() + 1)


@pytest.mark.parametrize("code", [3, 1])
def test_execute_fixed_rejection_then_input_uses_worker_and_preserves_trace(
    tmp_path, monkeypatch, code
):
    from rustyera_tui import test_cli
    from rustyera_tui.runtime import FrontendEvent, PresentationBatch

    session, snapshot, calls, _ = rejection_session(tmp_path, code=code)
    session._last_wait = None
    session.project_load_reports = []
    session.model.lines = [SimpleNamespace(segments=[SimpleNamespace(text="READY")])]
    session.worker.events.put(
        FrontendEvent(
            "presentation_batch",
            PresentationBatch(None, None, session.worker.client.active_wait, True),
        )
    )
    send = session.worker.send

    def submit_and_continue(operation, value=None):
        send(operation, value)
        if operation == "submit_text":
            assert value == "7"
            session.model.lines.append(
                SimpleNamespace(segments=[SimpleNamespace(text="CONTINUED")])
            )
            wait = {**session.worker.client.active_wait, 0: 10, 11: {0: 3, 1: 10}}
            session.worker.client.active_wait = wait
            session.worker.events.put(
                FrontendEvent("presentation_batch", PresentationBatch(None, None, wait, True))
            )

    session.worker.send = submit_and_continue
    session.worker.stop = lambda: None
    session.worker.join = lambda timeout: None
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.erb").write_text("@SYSTEM_TITLE\nINPUT\nRETURN\n")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "project",
                "seed": 1,
                "inputs": [
                    {
                        "action": "reject_snapshot",
                        "path": snapshot.name,
                        "expect_rejection": {"code": 3, "context": None},
                    },
                    7,
                ],
                "goal": {"output_contains": ["CONTINUED"], "wait_kind": 2},
                "limits": {"max_steps": 3, "timeout_seconds": 5},
            }
        )
    )
    trace = tmp_path / "trace.ndjson"
    for name in (
        "RUSTYERA_TEST_COMPILED_CACHE_INPUT",
        "RUSTYERA_TEST_COMPILED_CACHE_OUTPUT",
        "RUSTYERA_TEST_SOURCE_INDEX_INPUT",
        "RUSTYERA_TEST_SOURCE_INDEX_OUTPUT",
        "RUSTYERA_TEST_PROJECT_OUTPUT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(test_cli, "discover_library", lambda *_args: tmp_path / "unused-library")
    monkeypatch.setattr(test_cli, "RustTestSession", lambda *_args, **_kwargs: session)
    args = test_cli.build_parser().parse_args(
        ["run", "--scenario", str(scenario), "--trace", str(trace)]
    )
    if code == 3:
        assert test_cli.execute(args) == 0
        assert calls == [("restore_snapshot", snapshot), ("submit_text", "7")]
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        rejected = next(record for record in records if record["type"] == "snapshot_rejected")
        assert rejected["rejection"]["code"] == 3
        assert rejected["rejection"]["context"] is None
        assert rejected["rejection"]["correlation_id"] == 21
        assert rejected["wait_preserved"] is True
        assert records[-1]["status"] == "passed"
        assert any(record.get("goal", {}).get("satisfied") for record in records)
    else:
        with pytest.raises(TestDriverError, match="snapshot rejection differs"):
            test_cli.execute(args)
        assert calls == [("restore_snapshot", snapshot)]
        assert not any(
            json.loads(line).get("status") == "passed" for line in trace.read_text().splitlines()
        )


def test_project_report_observation_preserves_correlation_and_raw_diagnostic(tmp_path):
    from rustyera_tui.runtime import FrontendEvent, PresentationBatch

    session, _, _, _ = rejection_session(tmp_path)
    client = session.worker.client
    session.project_load_reports = []
    session._last_wait = None
    bundle = SimpleNamespace(revision=7, files={})
    bundle.materialize = lambda _progress: bundle
    client.pending_bundle = bundle
    client.bundle = None
    client._submit_project = lambda _cache: None
    client.record_host_duration = lambda *_args: None
    diagnostic = {
        0: "runtime.compiled_cache_ignored",
        1: 3,
        2: "provider-specific detail",
        3: None,
        4: {1: "compiled_cache", 2: "load"},
    }
    report = {0: 7, 1: False, 2: [diagnostic], 3: True}
    client._handle_project_report(report, 21)
    diagnostic[2] = "changed after dispatch"
    session.worker.events.put(
        FrontendEvent("presentation_batch", PresentationBatch(None, None, client.active_wait, True))
    )
    observation = session.wait_observation(time.monotonic() + 1)
    captured = observation["project_load_reports"][0]
    assert captured["correlation_id"] == 21
    assert captured["report"][3] is True
    assert captured["report"][2][0][0] == "runtime.compiled_cache_ignored"
    assert captured["report"][2][0][2] == "provider-specific detail"
    assert captured["report"][2][0][4] == {1: "compiled_cache", 2: "load"}


def test_trace_preserves_binary_project_report_identity(tmp_path):
    from io import StringIO

    from rustyera_tui.testing_trace import TraceWriter

    stream = StringIO()
    path = tmp_path / "trace.ndjson"
    event = {
        "type": "observation",
        "rust": {"project_load_reports": [{"report": {0: b"\x00\xff"}}]},
    }
    writer = TraceWriter(path, stream)
    try:
        writer.emit(event)
    finally:
        writer.close()
    persisted = json.loads(path.read_text())
    assert persisted == json.loads(stream.getvalue())
    assert persisted["rust"]["project_load_reports"][0]["report"]["0"] == {"cbor_bytes_hex": "00ff"}
    assert event["rust"]["project_load_reports"][0]["report"][0] == b"\x00\xff"


def test_snapshot_start_rejection_preserves_structured_correlation():
    client, _ = client_with_capture()
    client.begin_game_state_transition(29)
    client._handle_command_rejection({0: 3, 1: "profile mismatch"}, 29)
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    rejection = next(event.value for event in events if event.kind == "command_rejected")
    assert rejection["code"] == 3
    assert rejection["context"] is None
    assert rejection["correlation_id"] == 29
    assert rejection["import_purpose"] == "game_state"
    assert (
        next(event.value for event in events if event.kind == "runtime_error")
        == rejection["error_message"]
    )
