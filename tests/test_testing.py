from __future__ import annotations

import json
import queue
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rustyera_tui.runtime import FrontendEvent, PendingStateImport, PresentationBatch, RuntimeClient
from rustyera_tui.test_cli import build_parser, dispatch_agent_request
from rustyera_tui.testing import (
    RustTestSession,
    Scenario,
    TestDriverError,
    TraceWriter,
    apply_presentation_event,
    compare_observations,
    goal_status,
    install_test_compiled_cache,
    install_test_source_index,
    isolated_project_copy,
    normalized_lines,
    output_delta,
    publish_test_handoff,
)
from rustyera_tui.wire import unwrap_variant


def write_scenario(path: Path, project: Path, **values: Any) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": str(project),
                "mode": "fixed",
                **values,
            }
        ),
        encoding="utf-8",
    )


def test_scenario_uses_explicit_seed(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    path = tmp_path / "scenario.json"
    write_scenario(path, project, seed=17)

    assert Scenario.load(path).seed == 17


def test_scenario_accepts_a_full_u64_seed_as_a_decimal_string(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    path = tmp_path / "scenario.json"
    write_scenario(path, project, seed=str(0xFFFF_FFFF_FFFF_FFFF))

    assert Scenario.load(path).seed == 0xFFFF_FFFF_FFFF_FFFF


@pytest.mark.parametrize("seed", [1.5, True, "-1", "18446744073709551616"])
def test_scenario_rejects_non_u64_seed_values(tmp_path: Path, seed: object) -> None:
    project = tmp_path / "game"
    project.mkdir()
    path = tmp_path / "scenario.json"
    write_scenario(path, project, seed=seed)

    with pytest.raises(TestDriverError, match="decimal unsigned 64-bit"):
        Scenario.load(path)


def test_scenario_resolves_state_path_without_reseeding(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    save = tmp_path / "save00.sav"
    save.write_bytes(b"save")
    path = tmp_path / "scenario.json"
    write_scenario(
        path,
        project,
        start={"type": "traditional_save", "path": save.name},
        inputs=[1, {"value": "C", "when": {"output_contains": "menu"}}],
    )

    scenario = Scenario.load(path)

    assert scenario.seed is None
    assert scenario.start.path == save
    assert scenario.inputs == ({"value": 1}, {"value": "C", "when": {"output_contains": "menu"}})


def test_scenario_generates_and_exposes_seed_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "game"
    project.mkdir()
    path = tmp_path / "scenario.json"
    write_scenario(path, project)
    monkeypatch.setattr("rustyera_tui.testing.secrets.randbelow", lambda _limit: 123456)

    assert Scenario.load(path).seed == 123456


def test_scenario_rejects_snapshot_without_path(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    path = tmp_path / "scenario.json"
    write_scenario(path, project, start={"type": "vm_snapshot"})

    with pytest.raises(TestDriverError, match="requires path"):
        Scenario.load(path)


def test_scenario_accepts_rust_only_message_skip_action(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    path = tmp_path / "scenario.json"
    write_scenario(path, project, inputs=[{"action": "skip_message"}])

    assert Scenario.load(path).inputs == ({"action": "skip_message"},)

    write_scenario(
        path,
        project,
        inputs=[{"action": "skip_message"}],
        comparison={"reference": True},
    )
    with pytest.raises(TestDriverError, match="cannot be compared"):
        Scenario.load(path)


def test_reference_commands_are_accepted_as_quoted_command_lines() -> None:
    parsed = build_parser().parse_args(
        [
            "run",
            "--scenario",
            "scenario.json",
            "--reference-command",
            "wine Reference.exe",
            "--reference-path-command",
            "winepath -w",
        ]
    )

    assert parsed.reference_command == "wine Reference.exe"
    assert parsed.reference_path_command == "winepath -w"


def test_output_delta_and_normalization_cover_append_replace_and_noise() -> None:
    assert output_delta(["one", "two"], ["one", "three"]).as_dict() == {
        "reset": False,
        "removed": 1,
        "added": ["three"],
    }
    assert output_delta(["old"], ["new"]).reset
    assert normalized_lines(["same  \r", "Now Loading game"], [r"^Now Loading"]) == ["same"]


def test_isolated_project_copy_excludes_generated_rustyera_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "erb").mkdir(parents=True)
    (source / "erb" / "main.erb").write_text("@SYSTEM_TITLE\n", encoding="utf-8")
    (source / ".rustyera" / "cache").mkdir(parents=True)
    (source / ".rustyera" / "cache" / "index").write_text("generated", encoding="utf-8")

    copied = isolated_project_copy(source, tmp_path / "work", "rust")

    assert (copied / "erb" / "main.erb").is_file()
    assert not (copied / ".rustyera").exists()


def test_cross_host_cache_handoff_uses_only_the_isolated_test_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    incoming = tmp_path / "browser.reracache"
    incoming.write_bytes(b"browser-cache")
    source_file = project / "main.erb"
    source_file.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    portable_mtime_ms = 1_700_000_000_123
    incoming_index = tmp_path / "browser-source-index.json"
    incoming_index.write_text(
        json.dumps(
            {
                "version": 3,
                "files": {
                    "main.erb": {
                        "category": 2,
                        "signature": f"{source_file.stat().st_size}:{portable_mtime_ms}",
                        "hash": "00" * 32,
                        "size": source_file.stat().st_size,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    install_test_compiled_cache(project, incoming)
    install_test_source_index(project, incoming_index)

    installed = project / ".rustyera" / "cache" / "compiled-project.reracache"
    assert installed.read_bytes() == b"browser-cache"
    assert source_file.stat().st_mtime_ns // 1_000_000 == portable_mtime_ms
    outgoing = tmp_path / "tui.reracache"
    outgoing_index = tmp_path / "tui-source-index.json"
    session = object.__new__(RustTestSession)
    storage = type("Storage", (), {"compiled_cache_path": lambda _self: installed})()
    client = type("Client", (), {"storage": storage})()
    session.worker = type("Worker", (), {"client": client})()
    source_project = tmp_path / "source-project"
    source_project.mkdir()
    exported_project = tmp_path / "exported-project"
    publish_test_handoff(
        session,
        project,
        source_project,
        incoming,
        incoming_index,
        outgoing,
        outgoing_index,
        exported_project,
    )
    assert outgoing.read_bytes() == b"browser-cache"
    assert json.loads(outgoing_index.read_text(encoding="utf-8")) == json.loads(
        incoming_index.read_text(encoding="utf-8")
    )
    assert (exported_project / "main.erb").is_file()
    assert not (exported_project / ".rustyera").exists()

    with pytest.raises(TestDriverError, match="must not exist"):
        publish_test_handoff(
            session,
            project,
            source_project,
            incoming,
            incoming_index,
            outgoing,
            None,
            None,
        )


def test_agent_source_reload_operations_stay_inside_the_isolated_project(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "ERB" / "main.erb"
    source.parent.mkdir(parents=True)
    source.write_text("PRINTL VERSION=1\n", encoding="utf-8")
    commands: list[tuple[str, Any]] = []
    session = object.__new__(RustTestSession)
    session.project_root = project
    session.worker = SimpleNamespace(send=lambda kind, value=None: commands.append((kind, value)))

    session.edit_source("ERB/main.erb", "VERSION=1", "VERSION=2")
    session.reload("folder", "ERB")
    session.reload("file", "ERB/main.erb")
    session.reload("all")
    session.restart()

    assert source.read_text(encoding="utf-8") == "PRINTL VERSION=2\n"
    assert commands == [
        ("reload_folder", project / "ERB"),
        ("reload_file", source),
        ("reload_all", None),
        ("restart", None),
    ]
    with pytest.raises(TestDriverError, match="inside the isolated project"):
        session.edit_source("../outside.erb", "v1", "v2")


def test_agent_dispatch_classifies_non_step_and_advancing_operations(tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    rust = SimpleNamespace(
        inspect=lambda watches, deadline: calls.append(("inspect", (watches, deadline))) or {},
        wait_for_status=lambda text, deadline: calls.append(("wait", (text, deadline))) or text,
        edit_source=lambda path, expected, replacement: calls.append(
            ("edit", (path, expected, replacement))
        ),
        export_snapshot=lambda path: calls.append(("snapshot", path)),
        restart=lambda: calls.append(("restart", None)),
        reload=lambda scope, path: calls.append(("reload", (scope, path))),
        submit=lambda value: calls.append(("step", value)),
    )
    common = {
        "rust": rust,
        "reference_enabled": False,
        "deadline": 12.5,
        "step": 4,
        "trace_path": tmp_path / "trace.ndjson",
    }

    inspect = dispatch_agent_request({"op": "inspect", "watches": ["FLAG:0"]}, **common)
    status = dispatch_agent_request({"op": "wait_status", "text": "项目缓存已保存。"}, **common)
    edit = dispatch_agent_request(
        {
            "op": "edit_source",
            "path": "ERB/main.erb",
            "expected": "v1",
            "replacement": "v2",
        },
        **common,
    )
    reload = dispatch_agent_request(
        {"op": "reload", "scope": "folder", "path": "ERB/folder"}, **common
    )
    restart = dispatch_agent_request({"op": "restart"}, **common)
    step = dispatch_agent_request({"op": "step", "input": "1"}, **common)

    assert not inspect.advances and inspect.trace_event["type"] == "inspection"
    assert not status.advances and status.trace_event["type"] == "status_observed"
    assert status.observed_cache_save
    assert not edit.advances and edit.trace_event["type"] == "source_edited"
    assert reload.advances and not reload.drives_reference
    assert reload.completion_status == "脚本热重载完成"
    assert reload.trace_event == {
        "type": "runtime_action",
        "step": 5,
        "source": "agent",
        "action": "reload_folder",
        "path": "ERB/folder",
    }
    assert restart.advances and not restart.drives_reference
    assert step.advances and step.drives_reference and step.input_value == "1"
    assert calls == [
        ("inspect", (("FLAG:0",), 12.5)),
        ("wait", ("项目缓存已保存。", 12.5)),
        ("edit", ("ERB/main.erb", "v1", "v2")),
        ("reload", ("folder", "ERB/folder")),
        ("restart", None),
        ("step", "1"),
    ]

    with pytest.raises(TestDriverError, match="reference comparison"):
        dispatch_agent_request(
            {"op": "reload", "scope": "all"},
            **{**common, "reference_enabled": True},
        )


def test_reload_completion_status_republishes_the_stable_runtime_wait() -> None:
    active_wait = {0: 9, 1: 2, 2: 0, 5: True, 11: {0: 3, 1: 9}}
    session = object.__new__(RustTestSession)
    session.model = SimpleNamespace(lines=[])
    session.previous_output = []
    session.statuses = []
    session.logs = []
    session.metrics = []
    session._last_wait = (4, {0: 2, 1: 4})
    events: queue.Queue[FrontendEvent] = queue.Queue()
    events.put(FrontendEvent("status", "脚本热重载完成。"))
    session.worker = SimpleNamespace(
        events=events,
        is_alive=lambda: True,
        client=SimpleNamespace(phase=5, epoch=3, active_wait=active_wait),
    )

    observation = session.wait_observation(
        time.monotonic() + 1,
        completion_status="脚本热重载完成",
    )

    assert observation["epoch"] == 3
    assert observation["wait"]["id"] == 9
    assert observation["wait"]["kind"] == 2
    assert observation["statuses"] == ["脚本热重载完成。"]


def test_trace_keeps_full_output_while_agent_stream_uses_tail(tmp_path: Path) -> None:
    stream = StringIO()
    trace_path = tmp_path / "trace.ndjson"
    trace = TraceWriter(trace_path, stream)

    trace.emit(
        {
            "type": "observation",
            "rust": {
                "output": ["full"],
                "output_delta": {"reset": False, "removed": 0, "added": list(map(str, range(40)))},
                "output_tail": ["tail"],
            },
            "reference": {"output": ["reference"], "output_tail": ["reference-tail"]},
        }
    )
    trace.close()

    assert json.loads(trace_path.read_text(encoding="utf-8"))["rust"]["output"] == ["full"]
    streamed = json.loads(stream.getvalue())
    assert "output" not in streamed["rust"]
    assert streamed["rust"]["output_tail"] == ["tail"]
    assert streamed["rust"]["output_delta"]["added"] == list(map(str, range(10, 40)))
    assert streamed["rust"]["output_delta"]["added_omitted"] == 10


class FakeModel:
    def __init__(self) -> None:
        self.snapshots: list[dict[int, Any]] = []
        self.deltas: list[dict[int, Any]] = []

    def apply_snapshot(self, value: dict[int, Any]) -> None:
        self.snapshots.append(value)

    def apply_delta(self, value: dict[int, Any]) -> None:
        self.deltas.append(value)


def test_presentation_adapter_applies_atomic_batch_before_returning_wait() -> None:
    model = FakeModel()
    wait = {0: 7, 11: {0: 1, 1: 2}}

    returned = apply_presentation_event(
        model,  # type: ignore[arg-type]
        FrontendEvent("presentation_batch", PresentationBatch({0: 1}, {0: 2}, wait, True)),
    )

    assert returned == wait
    assert model.snapshots == [{0: 1}]
    assert model.deltas == [{0: 2}]


def test_comparison_uses_default_wait_map_and_reports_projection_difference() -> None:
    rust = {
        "wait": {"kind": 2},
        "output_delta": {"added": ["value  "]},
        "watches": {"FLAG:0": 1},
    }
    reference = {
        "wait": {"kind": "IntValue"},
        "output_delta": {"added": ["other"]},
        "watches": {"FLAG:0": 2},
    }

    result = compare_observations(rust, reference, {})

    assert not result["equal"]
    assert set(result["differences"]) == {"output_delta", "watches"}


def test_goal_combines_output_wait_watch_line_and_status_checks() -> None:
    observation = {
        "output": ["HOME [Look]"],
        "wait": {"kind": 2},
        "termination": "waitingInput",
        "watches": {"FLAG:0": 7},
        "statuses": ["编译缓存已保存。"],
    }
    goal = {
        "output_contains": ["[Look]"],
        "wait_kind": 2,
        "watch_equals": {"FLAG:0": 7},
        "line_count_lte": 1,
        "status_contains": ["缓存已保存"],
    }

    assert goal_status(observation, goal)["satisfied"]


def test_snake_data_scenario_requires_all_stages_and_preserves_ordinary_flag(
    tmp_path: Path,
) -> None:
    scenario_path = (
        Path(__file__).resolve().parents[1]
        / "tools/runtime-tester/scenarios/snake-data.json"
    )
    scenario = Scenario.load(scenario_path, project_override=tmp_path)
    assert scenario.inputs == (
        {"value": "1", "when": {"output_contains": "SNAKE_DATA_START"}},
    )
    observation = {
        "output": [
            "SNAKE_DATA_INDEX=2/main/42",
            "SNAKE_DATA_RESOURCE=1/1/0",
            "SNAKE_DATA_OVERLAY=1/1/1/2",
            "SNAKE_DATA_STRUCTURED=1/station/29/29/42/from-schema",
            "SNAKE_DATA_GLOBAL_MISSING=0/66/55",
            "SNAKE_DATA_GLOBAL=1/7/55/1/12/saved-map/saved-xml",
            "SNAKE_DATA_READY",
        ],
        "wait": {"kind": 2},
        "watches": {"GLOBAL:0": 7, "FLAG:0": 55, "C1_METHOD_VALUE:0": 42},
    }

    assert goal_status(observation, scenario.goal)["satisfied"]
    assert not goal_status(
        {**observation, "output": ["SNAKE_DATA_READY"]}, scenario.goal
    )["satisfied"]
    assert not goal_status(
        {**observation, "watches": {**observation["watches"], "FLAG:0": 8}}, scenario.goal
    )["satisfied"]


def test_runtime_start_request_uses_configured_seed() -> None:
    client = object.__new__(RuntimeClient)
    client.new_game_seed = 42

    tag, fields = unwrap_variant(client._new_game_start()[0])

    assert tag == 0
    assert fields == [42]


def test_runtime_routes_import_ready_by_state_kind() -> None:
    client = object.__new__(RuntimeClient)
    client.pending_import = PendingStateImport(
        kind=0,
        purpose="traditional_save",
        total_bytes=5,
        payload=b"state",
        transfer_id=9,
    )
    client.pending_restore = (Path("state"), b"state", "traditional_save")
    client.events = SimpleNamespace(put=lambda _event: None)
    captured: list[tuple[int, dict[int, Any]]] = []
    client.send_runtime = lambda tag, value: captured.append((tag, value)) or 41  # type: ignore[method-assign]
    transitions: list[int | None] = []
    client.begin_game_state_transition = transitions.append  # type: ignore[method-assign]

    client._handle_import_ready({0: 9, 1: 0})

    start_tag, start_fields = unwrap_variant(captured[0][1][0])
    assert captured[0][0] == 20
    assert start_tag == 1
    assert start_fields == [9]
    assert transitions == [41]
