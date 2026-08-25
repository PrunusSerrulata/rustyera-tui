from __future__ import annotations

from services_test_support import (
    Any,
    AtomicExportStream,
    DiagnosisExport,
    DiagnosisProgress,
    ExportStage,
    FrontendEvent,
    Path,
    SimpleNamespace,
    blake3,
    client_with_capture,
    next_event_of_kind,
    pytest,
    runtime_module,
    variant,
)
from rustyera_tui.runtime_export import PendingStateImport


def test_full_project_export_preempts_cache_and_cleans_up_on_cancel(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    manifest = {0: 1, 1: []}
    manifest_bytes = runtime_module.encode(manifest)
    manifest_path = tmp_path / "full-manifest.cbor"
    manifest_path.write_bytes(manifest_bytes)
    client.bundle = SimpleNamespace(
        project_file=None,
        write_full_manifest_temp=lambda _progress, _cancelled: (
            manifest_path,
            len(manifest_bytes),
        ),
    )
    client.pending_export_kind = 2
    client.cache_preparation_started = True
    client.pending_cache_stream = AtomicExportStream.open(tmp_path / "cache.reracache")
    target = tmp_path / "full.reraproj"

    client.export_project_file(target, lambda: False)

    assert [tag for tag, _value in captured] == [71, 62]
    assert captured[0][1] == {0: 2}
    assert captured[1][1] == {0: 5, 1: len(manifest_bytes)}
    client.maybe_refresh_compiled_cache()
    assert [tag for tag, _value in captured] == [71, 62]
    client._handle_import_accepted({0: 9})
    assert captured[-1][0] == 65
    assert captured[-1][1][0] == 9
    assert captured[-1][1][1] == blake3.blake3(manifest_bytes).digest()
    client._handle_import_ready({0: 9, 1: 5})
    assert captured[-1] == (60, {0: 3, 1: 0})
    assert client.pending_import is None
    assert client.full_project_export is not None
    temporary = client.full_project_export.stream.temporary
    assert temporary.exists()
    assert client.cache_refresh_pending

    client.cancel_project_file_export()

    assert captured[-1] == (71, {0: 3})
    assert client.full_project_export is None
    assert not temporary.exists()
    events = [client.events.get_nowait() for _ in range(client.events.qsize())]
    assert FrontendEvent("project_file_export_finished", None) in events
    assert FrontendEvent("project_progress_finished") in events


def test_diagnosis_manifest_import_rejection_cancels_transfer_and_cleans_state(
    tmp_path: Path,
) -> None:
    client, captured = client_with_capture()
    temporary = tmp_path / "manifest.cbor"
    temporary.write_bytes(b"pending")
    client.pending_import = PendingStateImport(
        kind=5,
        purpose="diagnosis_project_export",
        total_bytes=7,
        path=temporary,
        begin_message_id=11,
        transfer_id=9,
        command_message_ids={11, 12, 13},
    )
    client.pending_diagnosis = DiagnosisExport(
        target=tmp_path / "diagnosis.zip",
        project_name="game",
        logs="",
    )

    client._handle_command_rejection({0: 2, 1: "bad chunk"}, 12)

    assert client.pending_import is None
    assert client.pending_diagnosis is None
    assert not temporary.exists()
    assert (69, {0: 9}) in captured
    event = next_event_of_kind(client, "diagnosis_export_finished")
    assert event.value == (False, "状态导入命令被拒绝：bad chunk")


def test_snapshot_export_purposes_and_restore_warnings_are_frontend_visible(
    tmp_path: Path,
) -> None:
    client, captured = client_with_capture()
    client.pending_export = None
    client.pending_export_kind = None
    client.pending_export_message = None
    client.pending_diagnosis = None
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")

    client.export_snapshot(tmp_path / "debug.snapshot", "debug")
    assert captured.pop() == (60, {0: 1, 1: 1})

    client.pending_export = None
    client.pending_export_kind = None
    client.pending_export_message = None
    client.export_diagnosis(
        tmp_path / "diagnosis.tar.zst",
        "complete log\n",
        "eraThe World",
    )
    assert captured.pop() == (60, {0: 4, 1: 0})

    client._handle_runtime(
        97,
        {
            0: "runtime.snapshot_restored_from_diagnosis",
            1: 2,
            2: "restored a VM snapshot captured for diagnosis",
        },
        None,
    )
    assert next_event_of_kind(client, "log").kind == "log"
    warning = next_event_of_kind(client, "snapshot_restore_warning")
    assert warning.kind == "snapshot_restore_warning"
    assert "诊断信息" in warning.value


def test_operation_sequence_export_reuses_the_state_transfer_pipeline(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    target = tmp_path / "input-replay.jsonl"
    data = b'{"record":"header"}\n'

    client.export_input_replay(target)

    assert captured.pop() == (60, {0: 4, 1: 0})
    assert client.pending_export_kind == ExportStage.INPUT_REPLAY
    descriptor = {0: 17, 1: 4, 2: len(data), 3: blake3.blake3(data).digest()}
    client._handle_export_ready({0: 4, 1: variant(0, descriptor)}, 1)
    assert captured.pop() == (67, {0: 17, 1: 0, 2: 16 * 1024 * 1024})

    client._handle_export_chunk({0: 17, 1: 0, 2: data, 3: True})

    assert target.read_bytes() == data
    assert next_event_of_kind(client, "input_replay_export_finished").value is True
    assert "操作序列已导出" in next_event_of_kind(client, "status").value


def test_manual_state_export_does_not_replace_an_active_transfer(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    active = (tmp_path / "cache.reracache", bytearray(b"active"), {0: 9})
    client.pending_export = active
    client.pending_export_kind = ExportStage.COMPILED_CACHE
    client.pending_export_message = 41

    with pytest.raises(RuntimeError, match="another state export is already active"):
        client.export_input_replay(tmp_path / "input-replay.jsonl")

    assert client.pending_export is active
    assert client.pending_export_kind == ExportStage.COMPILED_CACHE
    assert client.pending_export_message == 41
    assert captured == []


def test_manual_state_export_rolls_back_when_submission_fails(tmp_path: Path) -> None:
    client, _captured = client_with_capture()
    client.send_runtime = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport failed"))
    )

    with pytest.raises(RuntimeError, match="transport failed"):
        client.export_input_replay(tmp_path / "input-replay.jsonl")

    assert client.pending_export is None
    assert client.pending_export_kind is None
    assert client.pending_export_message is None


def test_diagnosis_export_waits_for_an_existing_state_transfer(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    client.pending_export = (tmp_path / "compiled.bin", bytearray(), None)
    client.pending_export_kind = 2

    client.export_diagnosis(
        tmp_path / "diagnosis.tar.zst",
        "fault log\n",
        "eraThe World",
    )

    assert captured == []
    assert client.pending_diagnosis is not None
    assert client.pending_diagnosis.stage == "export_wait"

    client.pending_export = None
    client.maybe_refresh_compiled_cache()

    assert captured == [(60, {0: 4, 1: 0})]
    assert client.pending_export_kind == 6
    assert client.pending_diagnosis.stage == "replay"


def test_diagnosis_transfer_progress_uses_the_runtime_descriptor_bytes(tmp_path: Path) -> None:
    client, _captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    payload = b"four"
    client.export_diagnosis(
        tmp_path / "diagnosis.tar.zst",
        "fault log\n",
        "eraThe World",
    )
    assert next_event_of_kind(client, "diagnosis_progress").value == DiagnosisProgress("waiting")
    assert next_event_of_kind(client, "diagnosis_progress").value == DiagnosisProgress(
        "input_replay"
    )

    descriptor = {
        0: 10,
        1: 4,
        2: len(payload),
        3: blake3.blake3(payload).digest(),
    }
    client._handle_export_ready({0: 4, 1: [0, [descriptor]]}, correlation_id=1)
    assert next_event_of_kind(client, "diagnosis_progress").value == DiagnosisProgress(
        "input_replay", 0, 4
    )

    client._handle_export_chunk({0: 10, 1: 0, 2: b"fo", 3: False})
    assert next_event_of_kind(client, "diagnosis_progress").value == DiagnosisProgress(
        "input_replay", 2, 4
    )


@pytest.mark.parametrize(
    ("ready", "correlation_id"),
    [
        (
            {
                0: 4,
                1: [0, [{0: 10, 1: 4, 2: 2, 3: blake3.blake3(b"{}").digest()}]],
            },
            99,
        ),
        (
            {
                0: 1,
                1: [0, [{0: 10, 1: 4, 2: 2, 3: blake3.blake3(b"{}").digest()}]],
            },
            1,
        ),
        (
            {
                0: 4,
                1: [0, [{0: 10, 1: 1, 2: 2, 3: blake3.blake3(b"{}").digest()}]],
            },
            1,
        ),
    ],
)
def test_diagnosis_ready_mismatch_cancels_and_restores_interaction(
    tmp_path: Path, ready: dict[int, Any], correlation_id: int
) -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    client.active_wait = {0: 7, 1: 3, 8: 1, 11: {0: 2, 1: 3}}
    client.export_diagnosis(tmp_path / "diagnosis.tar.zst", "fault\n", "eraThe World")

    client._handle_export_ready(ready, correlation_id=correlation_id)

    assert client.pending_diagnosis is None
    assert client.pending_export is None
    assert captured[-1] == (71, {0: 4})
    assert next_event_of_kind(client, "diagnosis_export_finished").kind == (
        "diagnosis_export_finished"
    )


@pytest.mark.parametrize(
    ("chunk", "descriptor"),
    [
        ({0: 99, 1: 0, 2: b"{}", 3: True}, {0: 10, 1: 4, 2: 2, 3: b"x" * 32}),
        ({0: 10, 1: 1, 2: b"{}", 3: True}, {0: 10, 1: 4, 2: 2, 3: b"x" * 32}),
        ({0: 10, 1: 0, 2: b"{}", 3: True}, {0: 10, 1: 4, 2: 3, 3: b"x" * 32}),
        ({0: 10, 1: 0, 2: b"{}", 3: True}, {0: 10, 1: 4, 2: 2, 3: b"x" * 32}),
    ],
)
def test_diagnosis_chunk_validation_failure_is_local_and_restores_interaction(
    tmp_path: Path, chunk: dict[int, Any], descriptor: dict[int, Any]
) -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    target = tmp_path / "diagnosis.tar.zst"
    client.export_diagnosis(target, "fault\n", "eraThe World")
    client.pending_export = (target, bytearray(), descriptor)

    client._handle_export_chunk(chunk)

    assert client.pending_diagnosis is None
    assert client.pending_export is None
    assert captured[-1] == (69, {0: 10})
    finished = next_event_of_kind(client, "diagnosis_export_finished")
    assert finished.kind == "diagnosis_export_finished"
    assert finished.value[0] is False


def test_diagnosis_cleanup_restores_interaction_when_cancel_send_fails(tmp_path: Path) -> None:
    client, _captured = client_with_capture()
    client.bundle = SimpleNamespace(root=tmp_path / "eraTW")
    target = tmp_path / "diagnosis.tar.zst"
    client.export_diagnosis(target, "fault\n", "eraThe World")
    client.pending_export = (
        target,
        bytearray(),
        {0: 10, 1: 4, 2: 2, 3: blake3.blake3(b"{}").digest()},
    )

    def fail_send(*_args: object, **_kwargs: object) -> None:
        raise OSError("cancel failed")

    client.send_runtime = fail_send  # type: ignore[method-assign]
    client._finish_diagnosis_export(False, "failed")

    assert client.pending_diagnosis is None
    assert client.pending_export is None
    assert next_event_of_kind(client, "diagnosis_export_finished") == FrontendEvent(
        "diagnosis_export_finished", (False, "failed")
    )


def test_diagnosis_archive_failure_clears_progress_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _captured = client_with_capture()
    target = tmp_path / "diagnosis.tar.zst"
    payload = b"RERAPROJproject"
    client.pending_diagnosis = DiagnosisExport(
        target=target,
        project_name="eraFL",
        logs="fault\n",
        input_replay=b"replay",
        snapshot=b"snapshot",
        stage="project",
    )
    client.pending_export_kind = ExportStage.DIAGNOSIS_PROJECT
    client.pending_export = (
        target,
        bytearray(),
        {
            0: 12,
            1: 3,
            2: len(payload),
            3: blake3.blake3(payload).digest(),
        },
    )

    def fail_archive(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(runtime_module, "write_diagnosis_archive", fail_archive)

    client._handle_export_chunk({0: 12, 1: 0, 2: payload, 3: True})

    assert client.pending_diagnosis is None
    assert client.pending_export is None
    assert next_event_of_kind(client, "diagnosis_progress").value == DiagnosisProgress(
        "project_transfer", len(payload), len(payload)
    )
    assert next_event_of_kind(client, "diagnosis_progress").value == DiagnosisProgress("archive")
    assert next_event_of_kind(client, "diagnosis_export_finished") == FrontendEvent(
        "diagnosis_export_finished", (False, "disk full")
    )


def test_diagnosis_exports_a_full_project_and_retries_without_rescanning(
    tmp_path: Path,
) -> None:
    client, captured = client_with_capture()
    manifest = {0: 1, 1: []}
    manifest_bytes = runtime_module.encode(manifest)
    manifest_path = tmp_path / "full-manifest.cbor"
    manifest_path.write_bytes(manifest_bytes)
    materializations = 0

    def write_full_manifest(_progress: object, _cancelled: object) -> tuple[Path, int]:
        nonlocal materializations
        materializations += 1
        return manifest_path, len(manifest_bytes)

    client.bundle = SimpleNamespace(
        project_file=None,
        write_full_manifest_temp=write_full_manifest,
    )
    target = tmp_path / "diagnosis.tar.zst"
    snapshot = b"snapshot"
    input_replay = b'{"record":"header"}\n'
    project_file = b"RERAPROJproject"

    client.export_diagnosis(target, "complete log\n", "diagnosis fixture")
    replay_descriptor = {
        0: 10,
        1: 4,
        2: len(input_replay),
        3: blake3.blake3(input_replay).digest(),
    }
    client.pending_export = (target, bytearray(), replay_descriptor)
    client._handle_export_chunk({0: 10, 1: 0, 2: input_replay, 3: True})
    assert captured[-1] == (60, {0: 1, 1: 2})
    snapshot_descriptor = {
        0: 11,
        1: 3,
        2: len(snapshot),
        3: blake3.blake3(snapshot).digest(),
    }
    client.pending_export = (target, bytearray(), snapshot_descriptor)
    client._handle_export_chunk({0: 11, 1: 0, 2: snapshot, 3: True})

    assert materializations == 1
    assert captured[-1] == (62, {0: 5, 1: len(manifest_bytes)})
    client._handle_import_accepted({0: 9})
    client._handle_import_ready({0: 9, 1: 5})
    assert captured[-1] == (60, {0: 3, 1: 0})
    assert client.pending_diagnosis is not None
    assert client.pending_diagnosis.stage == "project"

    client._handle_runtime(95, {0: 0, 1: "full project preparation started"}, 1)
    assert client.pending_diagnosis is not None
    assert client.pending_diagnosis.stage == "project_wait"
    client.pending_diagnosis.retry_after_ns = 0
    client.maybe_refresh_compiled_cache()

    assert materializations == 1
    assert captured[-1] == (60, {0: 3, 1: 0})
    project_descriptor = {
        0: 12,
        1: 3,
        2: len(project_file),
        3: blake3.blake3(project_file).digest(),
    }
    client.pending_export = (target, bytearray(), project_descriptor)
    client._handle_export_chunk({0: 12, 1: 0, 2: project_file, 3: True})

    assert target.exists()
    assert client.pending_diagnosis is None
    assert client.pending_export_kind is None
    finished = next_event_of_kind(client, "diagnosis_export_finished")
    assert finished == FrontendEvent("diagnosis_export_finished", (True, str(target)))


def test_diagnosis_project_scan_failure_releases_the_export_state(tmp_path: Path) -> None:
    client, _captured = client_with_capture()

    def fail_materialize(_progress: object, _cancelled: object) -> None:
        raise OSError("scan failed")

    client.bundle = SimpleNamespace(
        project_file=None,
        write_full_manifest_temp=fail_materialize,
    )
    target = tmp_path / "diagnosis.tar.zst"
    snapshot = b"snapshot"
    input_replay = b'{"record":"header"}\n'

    client.export_diagnosis(target, "complete log\n", "diagnosis fixture")
    replay_descriptor = {
        0: 10,
        1: 4,
        2: len(input_replay),
        3: blake3.blake3(input_replay).digest(),
    }
    client.pending_export = (target, bytearray(), replay_descriptor)
    client._handle_export_chunk({0: 10, 1: 0, 2: input_replay, 3: True})
    descriptor = {
        0: 11,
        1: 3,
        2: len(snapshot),
        3: blake3.blake3(snapshot).digest(),
    }
    client.pending_export = (target, bytearray(), descriptor)
    client._handle_export_chunk({0: 11, 1: 0, 2: snapshot, 3: True})

    assert client.pending_diagnosis is None
    assert client.pending_export is None
    assert client.pending_export_kind is None
    assert next_event_of_kind(client, "diagnosis_export_finished") == FrontendEvent(
        "diagnosis_export_finished", (False, "scan failed")
    )
