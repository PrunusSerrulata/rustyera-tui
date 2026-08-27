from __future__ import annotations

import os

from compatibility_test_support import reference_identity
from rustyera_tui.presentation import PresentationModel
from rustyera_tui.testing import apply_presentation_event, plain_output

from runtime_cabi_test_support import (
    AbiError,
    DiagnosisProgress,
    EraCallHeader,
    FrontendEvent,
    Path,
    ProjectBundle,
    RuntimeAbi,
    RUNTIME_LIBRARY,
    RuntimeWorker,
    STATUS_INVALID_ARGUMENT,
    StorageBackend,
    _borrowed_bytes,
    _header,
    ctypes,
    json,
    pytest,
    shutil,
    tarfile,
    wait_for,
    wait_for_input,
    wait_for_path,
    zstandard,
)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_snake_ingestion_fixture_reaches_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "snake-ingestion"
    shutil.copytree(Path(__file__).parent / "fixtures" / "snake-ingestion-project", project)
    model = PresentationModel()
    logs: list[str] = []

    def reached_input(event: FrontendEvent) -> bool:
        if event.kind == "log":
            logs.append(str(event.value))
        return apply_presentation_event(model, event) is not None

    worker = RuntimeWorker(RUNTIME_LIBRARY, project, new_game_seed=123456)
    worker.start()
    try:
        try:
            wait_for(worker, reached_input)
        except AssertionError as error:
            raise AssertionError("\n".join(logs)) from error
        output = plain_output(model)
        assert "INGEST_FLAG=10,11,300" in output
        assert "INGEST_BUFF=50,60" in output
        assert "INGEST_ERD=70,80,90" in output
        assert "SNAKE_INGESTION_READY" in output
    finally:
        worker.stop()
        worker.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_relaunch_uses_the_persistent_compiled_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 0)
    project = Path(__file__).parent / "fixtures" / "minimal"
    cache_path = StorageBackend(project).compiled_cache_path()

    first = RuntimeWorker(RUNTIME_LIBRARY, project)
    first.start()
    try:
        wait_for(first, lambda event: event.kind == "project_loaded")
        wait_for_input(first)
        wait_for_path(first, cache_path)
    finally:
        first.stop()
        first.join(timeout=5)

    second = RuntimeWorker(RUNTIME_LIBRARY, project)
    second.start()
    try:
        cache_hit = wait_for(
            second,
            lambda event: event.kind == "log" and "runtime.compiled_cache_hit" in str(event.value),
        )
        assert "compiled_cache_hit" in cache_hit.value
        assert second.client is not None
        assert second.client.bundle is not None
        assert not second.client.bundle.is_materialized
        assert second.client.bundle.reload_baseline_pending
    finally:
        second.stop()
        second.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_abi_38_manifest_staging_reports_success_busy_and_invalid_cbor() -> None:
    with RuntimeAbi(RUNTIME_LIBRARY) as abi:
        assert abi.stage_project_manifest({0: 1, 1: [], 2: reference_identity()})
        with pytest.raises(AbiError, match="Busy"):
            abi.stage_project_manifest({0: 1, 1: [], 2: reference_identity()})

    malformed_values = (
        b"\xa2\x00\x01\x01\x81",  # truncated
        b"\xa2\x00\x01\x01\x80\x00",  # trailing data
        b"\xa2\x00\x18\x01\x01\x80",  # non-minimal integer
        b"\xa2\x01\x80\x00\x01",  # non-canonical map order
    )
    for malformed in malformed_values:
        with RuntimeAbi(RUNTIME_LIBRARY) as abi:
            status = abi._stage_project_manifest(
                _header(ctypes.sizeof(EraCallHeader)),
                abi.handle,
                _borrowed_bytes(malformed),
            )
            assert status == STATUS_INVALID_ARGUMENT


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_abi_cache_failure_retries_with_staged_source_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "minimal"
    shutil.copytree(Path(__file__).parent / "fixtures" / "minimal", project)
    ProjectBundle.scan_quick(project)
    cache_path = StorageBackend(project).compiled_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"invalid compiled cache")
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        ignored = wait_for(
            worker,
            lambda event: (
                event.kind == "log" and "runtime.compiled_cache_ignored" in str(event.value)
            ),
        )
        assert "compiled_cache_ignored" in str(ignored.value)
        wait_for(worker, lambda event: event.kind == "project_loaded")
        assert worker.client is not None and worker.client.bundle is not None
        assert worker.client.bundle.is_materialized
    finally:
        worker.stop()
        worker.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_exports_appends_and_reopens_packaged_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 0)
    project = tmp_path / "minimal"
    shutil.copytree(Path(__file__).parent / "fixtures" / "minimal", project)
    cache_path = StorageBackend(project).compiled_cache_path()
    project_file = tmp_path / "minimal.reraproj"
    source_worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    source_worker.start()
    try:
        wait_for(source_worker, lambda event: event.kind == "project_loaded")
        wait_for_input(source_worker)
        wait_for_path(source_worker, cache_path)
        source_worker.send("export_project_file", project_file)
        finished = wait_for(
            source_worker, lambda event: event.kind == "project_file_export_finished"
        )
        assert finished.value is True
        wait_for_path(source_worker, project_file)
    finally:
        source_worker.stop()
        source_worker.join(timeout=5)

    original = project_file.read_bytes()
    assert original.startswith(b"RERAPROJ")
    abi = RuntimeAbi(RUNTIME_LIBRARY, resource_directory=project)
    try:
        manifest = abi.project_file_manifest(original)
        bundle = ProjectBundle.from_project_file_manifest(project_file, manifest)
        bundle.write_configuration(
            b"",
            '[text]\nreplace_full_width_spaces = true\ncharacter_width_mode = "ambiguous_wide"\n',
            abi.prepare_project_configuration_update,
        )
    finally:
        abi.destroy_session()

    updated = project_file.read_bytes()
    assert updated.startswith(original)
    assert 0 < len(updated) - len(original) < 1024
    packaged_worker = RuntimeWorker(RUNTIME_LIBRARY, None, initial_project_file=project_file)
    packaged_worker.start()
    try:
        configuration = wait_for(packaged_worker, lambda event: event.kind == "configuration")
        snapshot, read_only = configuration.value
        assert read_only is False
        assert snapshot.effective_value("ReplaceFullWidthSpaces", "NO") == "YES"
        assert snapshot.effective_value("CharacterWidthMode", "AUTOMATIC") == "AMBIGUOUS_WIDE"
        wait_for_input(packaged_worker)
    finally:
        packaged_worker.stop()
        packaged_worker.join(timeout=5)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_diagnosis_contains_a_parseable_full_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "replay-diagnosis"
    shutil.copytree(Path(__file__).parent / "fixtures" / "replay-diagnosis", project)
    target = tmp_path / "replay-diagnosis.tar.zst"
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for(worker, lambda event: event.kind == "project_loaded")
        wait_for_input(worker)
        worker.send("submit_text", "7")
        wait_for_input(worker)
        script = project / "ERB" / "main.erb"
        script.write_text(
            script.read_text(encoding="utf-8").replace(
                "TUI_REPLAY_DIAGNOSIS_READY",
                "TUI_REPLAY_DIAGNOSIS_RELOADED",
            ),
            encoding="utf-8",
        )
        unselected = project / "ERB" / "unselected.erb"
        unselected.write_text(
            unselected.read_text(encoding="utf-8").replace(
                "UNSELECTED_ACTIVE",
                "UNSELECTED_DISK_ONLY",
            ),
            encoding="utf-8",
        )
        worker.send("reload_file", script)
        wait_for(
            worker,
            lambda event: event.kind == "status" and "热重载完成" in str(event.value),
        )
        wait_for_input(worker)
        worker.send("submit_text", "8")
        wait_for_input(worker)
        worker.send("export_diagnosis", (target, "complete log\n", "TUI Replay Diagnosis"))
        progress: list[DiagnosisProgress] = []

        def diagnosis_finished(event: FrontendEvent) -> bool:
            if event.kind == "diagnosis_progress":
                progress.append(event.value)
            return event.kind == "diagnosis_export_finished"

        finished = wait_for(worker, diagnosis_finished)
        assert finished.value == (True, str(target))
        stages = {item.stage for item in progress}
        assert {"input_replay", "vm_snapshot", "project_transfer", "archive"} <= stages
        assert all(item.total <= 0 or 0 <= item.completed <= item.total for item in progress)
        assert any(
            item.stage == "project_transfer" and item.total > 0 and item.completed == item.total
            for item in progress
        )
        assert any(
            item.stage == "archive" and item.total > 0 and item.completed == item.total
            for item in progress
        )
        wait_for_path(worker, target)
    finally:
        worker.stop()
        worker.join(timeout=5)

    with target.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                members = {
                    member.name: archive.extractfile(member).read()
                    for member in archive
                    if member.isfile()
                }
    project_file = next(value for name, value in members.items() if name.endswith(".reraproj"))
    replay = members["input-replay.jsonl"]
    header = json.loads(replay.splitlines()[0])
    assert header["record"] == "header"
    assert header["status"] == "available"
    assert header["origin"]["kind"] == "hot_reload"
    assert header["origin"]["before_revision"] == "1"
    assert header["origin"]["after_revision"] == "2"
    assert header["origin"]["changes"] == [
        {
            "operation": "upsert",
            "relative_path": "ERB/main.erb",
            "category": "erb",
        }
    ]
    assert header["step_count"] == 1
    step = json.loads(replay.splitlines()[1])
    assert step == {
        "record": "step",
        "sequence": 1,
        "action": "text",
        "wait_kind": "integer_value",
        "result": {"kind": "integer", "value": "8"},
        "message_skip": False,
        "text": "8",
    }
    assert project_file.startswith(b"RERAPROJ")
    abi = RuntimeAbi(RUNTIME_LIBRARY, resource_directory=project)
    try:
        manifest = abi.full_project_file_manifest(project_file)
    finally:
        abi.destroy_session()
    assert manifest[0] == 2
    submitted_sources = {}
    for file in manifest[1]:
        payload = file[2]
        if payload[0] == 0 and payload[1] and isinstance(payload[1][0], str):
            submitted_sources[str(file[0])] = payload[1][0]
    assert "TUI_REPLAY_DIAGNOSIS_RELOADED" in submitted_sources["ERB/main.erb"]
    assert "UNSELECTED_ACTIVE" in submitted_sources["ERB/unselected.erb"]
    assert "UNSELECTED_DISK_ONLY" not in submitted_sources["ERB/unselected.erb"]


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_loads_starts_and_serves_debug_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 0)
    project = Path(__file__).parent / "fixtures" / "minimal"
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for(worker, lambda event: event.kind == "project_loaded")
        wait = wait_for_input(worker)
        assert wait[1] == 0
        wait_for_path(worker, StorageBackend(project).compiled_cache_path())

        worker.send("restart")
        cache_hit = wait_for(
            worker,
            lambda event: event.kind == "log" and "runtime.compiled_cache_hit" in str(event.value),
        )
        assert "compiled_cache_hit" in cache_hit.value
        wait_for_input(worker)

        replay_path = tmp_path / "input-replay.jsonl"
        worker.send("export_input_replay", replay_path)
        replay_exported = wait_for(
            worker,
            lambda event: event.kind == "input_replay_export_finished",
        )
        assert replay_exported.value is True
        replay_header = json.loads(replay_path.read_text(encoding="utf-8").splitlines()[0])
        assert replay_header["record"] == "header"
        assert replay_header["fidelity"] == "manual_path"

        snapshot_path = tmp_path / "runtime.snapshot"
        worker.send("export_snapshot", (snapshot_path, "normal"))
        exported = wait_for(
            worker,
            lambda event: event.kind == "snapshot_export_finished",
        )
        assert exported.value is True
        assert snapshot_path.stat().st_size > 0

        with monkeypatch.context() as context:
            context.setattr(
                "rustyera_tui.runtime.ProjectBundle.scan",
                lambda *_args, **_kwargs: pytest.fail("snapshot restore must not scan the project"),
            )
            worker.send("restore_snapshot", snapshot_path)
            wait_for(
                worker,
                lambda event: event.kind == "status" and "正在恢复 VM" in str(event.value),
            )
            wait_for_input(worker)

        worker.send("reload_all")
        reloaded = wait_for(
            worker,
            lambda event: event.kind == "status" and "热重载完成" in str(event.value),
        )
        assert "完成" in reloaded.value

        worker.send("debug_enable")
        wait_for(worker, lambda event: event.kind == "debug_enabled" and event.value)
        worker.send("debug_action", ("variables", None))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        response = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "variables",
        )
        pending, response_tag, fields = response.value
        assert pending == "variables"
        assert response_tag == 1
        assert isinstance(fields[0].get(1), list)
        descriptor = fields[0][1][0]
        worker.send("debug_action", ("read_variable", descriptor))
        value = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "variable_value",
        )
        assert value.value[1] == 2

        worker.send("debug_action", ("fibers", None))
        fibers = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "fibers",
        ).value[2][0]
        assert fibers[1]
        fiber_id = fibers[1][0][0]
        worker.send("debug_action", ("call_stack", fiber_id))
        stack = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "call_stack",
        ).value[2][0]
        assert stack[2]

        worker.send("debug_action", ("console_evaluate", "1 + 2"))
        console = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "console",
        )
        assert console.value[1] == 8
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_single_step_crosses_input_wait_without_rejected_commands(
    tmp_path: Path,
) -> None:
    project = tmp_path / "single-step-project"
    project.mkdir()
    (project / "main.erb").write_text(
        "@SYSTEM_TITLE\nPRINTL before\nWAIT\nPRINTL after\nWAIT\nRETURN\n",
        encoding="utf-8",
    )
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for_input(worker)
        worker.send("debug_enable")
        wait_for(worker, lambda event: event.kind == "debug_enabled" and event.value)
        worker.send("debug_single_step", True)
        worker.send("submit_text", "")

        stops: list[FrontendEvent] = []
        for _ in range(8):
            stopped = wait_for(worker, lambda event: event.kind == "debug_stopped")
            stops.append(stopped)
            source = stopped.value.get(3)
            assert source is not None
            assert source.get(0) == "main.erb"
            if stopped.value[1][0] == 3:
                break
            worker.send("debug_step")
        else:
            pytest.fail("single stepping did not reach the next input host wait")

        wait_for(worker, lambda event: event.kind == "phase" and event.value == 5)

        worker.send("debug_single_step", False)
        worker.send("debug_action", ("console_execute", "RESULT = 7"))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        console = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "console",
        )
        assert console.value[1] == 8
        worker.send("debug_surface_closed", "console")
        wait_for(worker, lambda event: event.kind == "phase" and event.value == 5)

        worker.send("debug_action", ("pause_only", None))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        worker.send("debug_disable")
        resumed = wait_for(worker, lambda event: event.kind == "phase")
        assert resumed.value == 5
        disabled = wait_for(
            worker,
            lambda event: event.kind == "debug_enabled" and not event.value,
        )
        assert disabled.value is False
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_projects_three_channel_background_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "background-project"
    project.mkdir()
    (project / "main.erb").write_text(
        "@SYSTEM_TITLE\nSETBGCOLOR 1, 24, 60\nWAIT\nRESETBGCOLOR\nWAIT\nRETURN\n",
        encoding="utf-8",
    )
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        blue = {0: 1, 1: 24, 2: 60, 3: 255}
        black = {0: 0, 1: 0, 2: 0, 3: 255}
        initial = wait_for(
            worker,
            lambda event: (
                event.kind == "presentation_batch"
                and event.value.active_wait is not None
                and event.value.delta is not None
                and any(
                    operation[0] == 8 and operation[1][0].get(2) == blue
                    for operation in event.value.delta[2]
                )
            ),
        )
        assert initial.value.render
        worker.send("submit_text", "")
        wait_for(
            worker,
            lambda event: (
                event.kind == "presentation_batch"
                and event.value.delta is not None
                and any(
                    operation[0] == 8 and operation[1][0].get(2) == black
                    for operation in event.value.delta[2]
                )
            ),
        )
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_reports_a_terminal_runtime_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "fault-project"
    project.mkdir()
    (project / "main.erb").write_text(
        '@SYSTEM_TITLE\nRESULT = CSVNAME(999) == ""\nRETURN\n', encoding="utf-8"
    )
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        fault = wait_for(worker, lambda event: event.kind == "runtime_fault")
        assert "character CSV number 999 does not exist" in fault.value.message
        assert fault.value.function == "SYSTEM_TITLE"
    finally:
        worker.stop()
        worker.join(timeout=3)


@pytest.mark.skipif(
    os.environ.get("RUSTYERA_TEST_D_SERVICE_CABI") != "1",
    reason="1D service C ABI acceptance requires explicit opt-in after shared static gates",
)
@pytest.mark.parametrize(
    ("fixture", "expression", "service", "major", "api"),
    [
        ("html", 'RESULT = HTML_STRINGLEN("x",1)', "PresentationQuery.html_string_len", 2, "HTML_STRINGLEN"),
        ("html", 'RESULTS \'= HTML_SUBSTRING("x",1)', "PresentationQuery.html_substring", 2, "HTML_SUBSTRING"),
        ("html", 'RESULT = HTML_STRINGLINES("x",1)', "PresentationQuery.html_string_lines", 2, "HTML_STRINGLINES"),
        ("pointer", None, "InputState.pointer_state", 1, "MOUSEB"),
        ("canvas", None, "Canvas.sample_canvas_pixel", 1, "GGETCOLOR"),
    ],
    ids=("html-length-v2", "html-substring-v2", "html-lines-v2", "pointer-v1", "canvas-pixel-v1"),
)
def test_real_c_abi_snake_projection_services_report_exact_missing_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    expression: str | None,
    service: str,
    major: int,
    api: str,
) -> None:
    # Never discover a main-workspace library or skip a requested acceptance run.
    configured = os.environ.get("ERA_RUNTIME_LIBRARY")
    assert configured, "ERA_RUNTIME_LIBRARY must explicitly name the final 1D group C ABI library"
    library = Path(configured).expanduser().resolve(strict=True)
    assert library.is_file(), f"runtime library is not a file: {library}"
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "unsupported-project"
    shutil.copytree(
        Path(__file__).parent / "fixtures" / "snake-services-unsupported" / fixture, project
    )
    if expression is not None:
        source = project / "ERB" / "main.erb"
        text = source.read_text(encoding="utf-8")
        original = 'RESULT = HTML_STRINGLEN("x",1)'
        assert text.count(original) == 1
        source.write_text(text.replace(original, expression, 1), encoding="utf-8")

    model = PresentationModel()
    logs: list[str] = []

    def initial_wait(event: FrontendEvent) -> bool:
        if event.kind == "log":
            logs.append(str(event.value))
        return apply_presentation_event(model, event) is not None

    def terminal_fault(event: FrontendEvent) -> bool:
        if event.kind == "log":
            logs.append(str(event.value))
        wait = apply_presentation_event(model, event)
        assert wait is None, f"{api} unexpectedly returned to another input wait"
        assert event.kind != "worker_stopped", "worker stopped without a capability fault"
        return event.kind == "runtime_fault"

    worker = RuntimeWorker(library, project, new_game_seed=123456)
    worker.start()
    try:
        try:
            wait_for(worker, initial_wait)
            assert f"SNAKE_UNSUPPORTED_{fixture.upper()}" in plain_output(model)
            worker.send("submit_text", "0")
            fault = wait_for(worker, terminal_fault).value
        except AssertionError as error:
            raise AssertionError(f"{api}: {error}\n" + "\n".join(logs)) from error
        assert fault.code == 7  # Public FaultCode::UnsupportedRuntimeFeature.
        assert fault.function == "SYSTEM_TITLE"
        assert "profile=emuera.skia.snake@" in fault.compatibility
        assert f"requires={service}@{major}.0" in fault.compatibility
        assert "UnsupportedRuntimeFeature" in fault.display()
        assert "profile=emuera.skia.snake@" in fault.display()
        assert f"requires={service}@{major}.0" in fault.display()
        assert "SNAKE_UNSUPPORTED_UNEXPECTED_SUCCESS" not in plain_output(model)
        (tmp_path / "service-fault-evidence.json").write_text(
            json.dumps(
                {
                    "seed": 123456,
                    "runtime_library": str(library),
                    "requested_core_revision": (
                        Path(__file__).parents[1] / "rustyera-core.rev"
                    ).read_text(encoding="utf-8").strip(),
                    "api": api,
                    "required_service": f"{service}@{major}.0",
                    "output": plain_output(model),
                    "fault_code": fault.code,
                    "fault_display": fault.display(),
                    "fault_context": fault.compatibility,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    finally:
        worker.stop()
        worker.join(timeout=5)
        assert not worker.is_alive(), "real RuntimeWorker did not release its C ABI session"
