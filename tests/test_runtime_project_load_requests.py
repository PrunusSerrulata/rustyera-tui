"""Preserve the actual load command identity across cache and source requests."""

import queue
from types import SimpleNamespace

import pytest

from rustyera_tui.runtime_project import _RuntimeProjectMixin


@pytest.mark.parametrize("materialized,transfer", [(False, 17), (True, None)])
def test_project_submission_observation_uses_actual_command_id(materialized, transfer):
    identity = {0: 31, 1: [2, 3]}
    sent = []

    def send(tag, request):
        sent.append((tag, request))
        return 91

    bundle = SimpleNamespace(
        identity=lambda: identity,
        is_materialized=materialized,
        manifest=lambda: {0: "manifest"},
    )
    client = SimpleNamespace(
        pending_bundle=bundle,
        events=queue.Queue(),
        abi=SimpleNamespace(),
        send_runtime=send,
        record_host_duration=lambda *_: None,
    )
    _RuntimeProjectMixin._submit_project(client, transfer)
    client.events.get_nowait()  # Existing status notification.
    event = client.events.get_nowait()
    assert event.kind == "project_load_submitted"
    assert event.value == {
        "message_id": 91,
        "identity": identity,
        "has_source": materialized,
        "cache_transfer_id": transfer,
    }
    assert sent[0][0] == 19
    assert (1 in sent[0][1]) is materialized
    assert sent[0][1].get(2) == transfer
    identity[1].append(4)
    assert event.value["identity"][1] == [2, 3]


def test_snake_test_cache_uses_existing_profile_storage_namespace(tmp_path, monkeypatch):
    from rustyera_tui.storage import StorageBackend
    from rustyera_tui.testing import install_test_compiled_cache

    monkeypatch.delenv("ERA_TUI_DATA_DIR", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    incoming = tmp_path / "old-cache"
    incoming.write_bytes(b"opaque-runtime-cache")
    install_test_compiled_cache(project, incoming, compatibility_profile="emuera.skia.snake")
    actual = StorageBackend(
        project, compatibility_profile="emuera.skia.snake"
    ).compiled_cache_path()
    assert actual.read_bytes() == b"opaque-runtime-cache"
    assert not StorageBackend(project).compiled_cache_path().exists()
