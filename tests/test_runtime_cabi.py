from __future__ import annotations

import blake3

from runtime_cabi_test_support import (
    DEFAULT_MAXIMUM_VM_INSTRUCTIONS,
    Path,
    ProjectBundle,
    RuntimeClient,
    StorageBackend,
    discover_library,
    pytest,
    queue,
)
from rustyera_tui.runtime import PendingStateImport


def test_default_drive_budget_keeps_the_caller_pump_cooperative() -> None:
    assert DEFAULT_MAXIMUM_VM_INSTRUCTIONS == 100_000


def test_compiled_cache_uses_the_abi_staging_fast_path() -> None:
    client = object.__new__(RuntimeClient)
    staged: list[bytes] = []
    submitted: list[int] = []
    fallback: list[tuple[bytes, int, str]] = []
    client.abi = type(
        "Abi",
        (),
        {"stage_compiled_cache": lambda _self, payload: staged.append(payload) or 41},
    )()
    client._submit_project = submitted.append  # type: ignore[method-assign]
    client._begin_import = lambda payload, kind, purpose: fallback.append(  # type: ignore[method-assign]
        (payload, kind, purpose)
    )

    client._stage_project_cache(b"cache", "project_cache")

    assert staged == [b"cache"]
    assert submitted == [41]
    assert fallback == []


def test_compiled_cache_falls_back_to_protocol_import_for_an_older_abi() -> None:
    client = object.__new__(RuntimeClient)
    submitted: list[int] = []
    fallback: list[tuple[bytes, int, str]] = []
    client.abi = type("Abi", (), {"stage_compiled_cache": lambda _self, _payload: None})()
    client._submit_project = submitted.append  # type: ignore[method-assign]
    client._begin_import = lambda payload, kind, purpose: fallback.append(  # type: ignore[method-assign]
        (payload, kind, purpose)
    )

    client._stage_project_cache(b"cache", "project_cache")

    assert submitted == []
    assert fallback == [(b"cache", 2, "project_cache")]


def test_compiled_cache_file_uses_runtime_owned_staging_without_python_bytes() -> None:
    client = object.__new__(RuntimeClient)
    staged: list[Path] = []
    submitted: list[int] = []
    client.abi = type(
        "Abi",
        (),
        {"stage_compiled_cache_file": lambda _self, path: staged.append(path) or 43},
    )()
    client._submit_project = submitted.append  # type: ignore[method-assign]

    path = Path("cache-does-not-need-to-be-read-by-runtime-client")
    client._stage_project_cache_file(path, "project_cache")

    assert staged == [path]
    assert submitted == [43]


def test_compiled_cache_file_falls_back_to_bytes_for_abi_34(tmp_path: Path) -> None:
    client = object.__new__(RuntimeClient)
    path = tmp_path / "compiled-project.reraproj"
    path.write_bytes(b"cache")
    staged: list[bytes] = []
    submitted: list[int] = []
    client.abi = type(
        "Abi",
        (),
        {
            "stage_compiled_cache_file": lambda _self, _path: None,
            "stage_compiled_cache": lambda _self, payload: staged.append(payload) or 44,
        },
    )()
    client._submit_project = submitted.append  # type: ignore[method-assign]

    client._stage_project_cache_file(path, "project_cache")

    assert staged == [b"cache"]
    assert submitted == [44]


def test_compiled_cache_file_io_failure_falls_back_to_materialized_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    bundle = ProjectBundle.scan_quick(tmp_path)
    cache_path = StorageBackend(tmp_path).compiled_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"cache")
    client = object.__new__(RuntimeClient)
    client.pending_bundle = bundle
    client.storage = StorageBackend(tmp_path)
    client.allow_compiled_cache_load = True
    client.events = queue.Queue()

    def fail_stage(_self: object, _path: Path) -> int:
        raise OSError("short read")

    client.abi = type(
        "Abi",
        (),
        {"stage_compiled_cache_file": fail_stage},
    )()
    client._project_scan_progress = lambda _completed, _total: None  # type: ignore[method-assign]
    submitted: list[int | None] = []
    client._submit_project = submitted.append  # type: ignore[method-assign]

    client._stage_persistent_cache_or_source()

    assert client.pending_bundle.is_materialized
    assert submitted == [None]
    events = list(client.events.queue)
    assert any(event.kind == "log" and "short read" in str(event.value) for event in events)


def test_runtime_library_is_discovered_in_the_resource_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_RUNTIME_LIBRARY", raising=False)
    monkeypatch.setattr("rustyera_tui.abi.sys.platform", "darwin")
    library = tmp_path / "libera_runtime_capi.dylib"
    library.write_bytes(b"library")

    assert discover_library(resource_directory=tmp_path) == library


def test_runtime_library_is_discovered_beside_the_packaged_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_RUNTIME_LIBRARY", raising=False)
    monkeypatch.setattr("rustyera_tui.abi.sys.platform", "darwin")
    module = tmp_path / "rustyera_tui" / "abi.py"
    module.parent.mkdir()
    module.write_text("", encoding="utf-8")
    library = module.parent / "libera_runtime_capi.dylib"
    library.write_bytes(b"library")
    monkeypatch.setattr("rustyera_tui.abi.__file__", str(module))

    assert discover_library(resource_directory=tmp_path) == library


def test_compiled_cache_persistence_waits_until_the_deferred_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.cache_refresh_pending = True
    client.cache_ready = False
    client.cache_refresh_after_ns = 100
    client.cache_refresh_after = "background"
    client.pending_export = None
    client.full_project_export = None
    client.pending_diagnosis = None
    requested: list[str] = []
    client._refresh_compiled_cache = requested.append  # type: ignore[method-assign]

    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 99)
    client.maybe_refresh_compiled_cache()
    assert requested == []

    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 100)
    client.maybe_refresh_compiled_cache()
    assert requested == ["background"]


def test_gameplay_input_defers_pending_cache_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(RuntimeClient)
    client.cache_refresh_pending = True
    client.cache_refresh_after_ns = 100

    monkeypatch.setattr("rustyera_tui.runtime.COMPILED_CACHE_PERSIST_DELAY_NS", 500)
    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 50)
    client.defer_compiled_cache_refresh()

    assert client.cache_refresh_after_ns == 550

    client.cache_refresh_pending = False
    monkeypatch.setattr("rustyera_tui.runtime.time.monotonic_ns", lambda: 1_000)
    client.defer_compiled_cache_refresh()
    assert client.cache_refresh_after_ns == 550


def test_state_import_uses_large_contiguous_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(RuntimeClient)
    client.pending_import = PendingStateImport(
        kind=1,
        purpose="snapshot",
        total_bytes=9,
        payload=b"123456789",
    )
    commands: list[tuple[int, dict[int, object]]] = []
    client.send_runtime = lambda tag, value: commands.append((tag, value))  # type: ignore[method-assign]
    monkeypatch.setattr("rustyera_tui.runtime.STATE_IMPORT_CHUNK_BYTES", 4)

    client._handle_import_accepted({0: 7})

    assert [(tag, value.get(1), len(value.get(2, b""))) for tag, value in commands] == [
        (64, 0, 4),
        (64, 4, 4),
        (64, 8, 1),
        (65, None, 0),
    ]


def test_full_manifest_file_import_uses_exact_four_mib_chunks(tmp_path: Path) -> None:
    payload = b"x" * (4 * 1024 * 1024) + b"end"
    path = tmp_path / "manifest.cbor"
    path.write_bytes(payload)
    client = object.__new__(RuntimeClient)
    client.pending_import = PendingStateImport(
        kind=5,
        purpose="full_project_export",
        total_bytes=len(payload),
        path=path,
    )
    commands: list[tuple[int, dict[int, object]]] = []
    client.send_runtime = lambda tag, value: commands.append((tag, value)) or len(commands)  # type: ignore[method-assign]

    client._handle_import_accepted({0: 11})

    assert [(tag, value.get(1), len(value.get(2, b""))) for tag, value in commands] == [
        (64, 0, 4 * 1024 * 1024),
        (64, 4 * 1024 * 1024, 3),
        (65, blake3.blake3(payload).digest(), 0),
    ]
    assert not path.exists()
