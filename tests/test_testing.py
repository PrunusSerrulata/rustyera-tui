from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rustyera_tui.runtime import FrontendEvent, PresentationBatch, RuntimeClient
from rustyera_tui.test_cli import build_parser
from rustyera_tui.testing import (
    Scenario,
    TestDriverError,
    TraceWriter,
    apply_presentation_event,
    compare_observations,
    goal_status,
    isolated_project_copy,
    normalized_lines,
    output_delta,
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


def test_runtime_start_request_uses_configured_seed() -> None:
    client = object.__new__(RuntimeClient)
    client.new_game_seed = 42

    tag, fields = unwrap_variant(client._new_game_start()[0])

    assert tag == 0
    assert fields == [42]


def test_runtime_routes_import_ready_by_state_kind() -> None:
    client = object.__new__(RuntimeClient)
    client.import_transfer_id = 9
    client.import_bytes = b"state"
    client.pending_restore = (Path("state"), b"state", "traditional_save")
    client.import_purpose = "traditional_save"
    client.events = SimpleNamespace(put=lambda _event: None)
    captured: list[tuple[int, dict[int, Any]]] = []
    client.send_runtime = lambda tag, value: captured.append((tag, value))  # type: ignore[method-assign]

    client._handle_import_ready({0: 9})

    start_tag, start_fields = unwrap_variant(captured[0][1][0])
    assert captured[0][0] == 20
    assert start_tag == 1
    assert start_fields == [9]
