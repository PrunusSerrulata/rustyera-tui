from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rustyera_tui.abi import (
    STATUS_BUSY,
    STATUS_INVALID_ARGUMENT,
    STATUS_OK,
    AbiError,
    EraOwnedBuffer,
    EraSessionHandle,
    RuntimeAbi,
)
from rustyera_tui.wire import encode, variant


class ManifestDecoder:
    def __init__(self, manifest: dict[int, Any]) -> None:
        self.manifest = manifest
        self.calls = 0
        self._buffers: list[Any] = []

    def __call__(self, _header: Any, _handle: Any, _input: Any, output: Any) -> int:
        self.calls += 1
        payload = encode(self.manifest)
        buffer = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
        self._buffers.append(buffer)
        owned = ctypes.cast(output, ctypes.POINTER(EraOwnedBuffer)).contents
        owned.data = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))
        owned.len = len(payload)
        owned.token = self.calls
        return STATUS_OK


def runtime_abi(frontend: ManifestDecoder | None, legacy: ManifestDecoder) -> RuntimeAbi:
    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._decode_project_file_frontend = frontend
    abi._decode_project_file = legacy
    abi._release = lambda _header, _buffer: STATUS_OK
    return abi


def test_abi_33_prefers_compact_frontend_project_manifest() -> None:
    compact = {0: 1, 1: [{0: "main.erb", 1: 2, 2: variant(0, ""), 3: bytes(32)}]}
    frontend = ManifestDecoder(compact)
    legacy = ManifestDecoder({0: 1, 1: []})

    decoded = runtime_abi(frontend, legacy).project_file_manifest(b"package")

    assert decoded == compact
    assert frontend.calls == 1
    assert legacy.calls == 0


def test_abi_32_falls_back_to_full_legacy_project_manifest() -> None:
    full = {0: 1, 1: [{0: "main.erb", 1: 2, 2: variant(0, "@MAIN\nRETURN\n")}]}
    legacy = ManifestDecoder(full)

    decoded = runtime_abi(None, legacy).project_file_manifest(b"package")

    assert decoded == full
    assert legacy.calls == 1


def test_abi_34_stages_a_contiguous_compiled_cache() -> None:
    captured: list[bytes] = []

    def stage(_header: Any, _handle: Any, input_value: Any, output: Any) -> int:
        captured.append(ctypes.string_at(input_value.data, input_value.len))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64)).contents.value = 27
        return STATUS_OK

    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._stage_compiled_cache = stage
    abi._check = lambda status, _operation: None if status == STATUS_OK else None

    assert abi.stage_compiled_cache(b"contiguous-cache") == 27
    assert captured == [b"contiguous-cache"]


def test_abi_33_without_the_reserved_entry_reports_no_staging_support() -> None:
    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._stage_compiled_cache = None

    assert abi.stage_compiled_cache(b"cache") is None


def test_abi_35_reads_a_cache_into_runtime_owned_memory(tmp_path: Path) -> None:
    cache = tmp_path / "compiled-project.reraproj"
    cache.write_bytes(b"runtime-owned-cache")
    allocations: list[Any] = []
    committed: list[bytes] = []

    def allocate(_header: Any, _handle: Any, length: int, output: Any) -> int:
        storage = (ctypes.c_uint8 * length)()
        allocations.append(storage)
        owned = ctypes.cast(output, ctypes.POINTER(EraOwnedBuffer)).contents
        owned.data = ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint8))
        owned.len = length
        owned.token = 91
        return STATUS_OK

    def commit(_header: Any, _handle: Any, buffer: EraOwnedBuffer, output: Any) -> int:
        committed.append(ctypes.string_at(buffer.data, buffer.len))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64)).contents.value = 92
        return STATUS_OK

    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._allocate_compiled_cache = allocate
    abi._commit_compiled_cache = commit
    abi._release = lambda _header, _buffer: STATUS_OK
    abi._check = lambda status, _operation: assert_status_ok(status)

    assert abi.stage_compiled_cache_file(cache) == 92
    assert committed == [b"runtime-owned-cache"]


def test_older_abi_reports_no_runtime_owned_file_buffer(tmp_path: Path) -> None:
    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi._allocate_compiled_cache = None
    abi._commit_compiled_cache = None

    assert abi.stage_compiled_cache_file(tmp_path / "not-read") is None


def test_abi_35_releases_a_writable_buffer_after_a_short_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "compiled-project.reraproj"
    cache.write_bytes(b"short")
    allocations: list[Any] = []
    released: list[int] = []

    def allocate(_header: Any, _handle: Any, length: int, output: Any) -> int:
        storage = (ctypes.c_uint8 * length)()
        allocations.append(storage)
        owned = ctypes.cast(output, ctypes.POINTER(EraOwnedBuffer)).contents
        owned.data = ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint8))
        owned.len = length
        owned.token = 93
        return STATUS_OK

    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._allocate_compiled_cache = allocate
    abi._commit_compiled_cache = lambda *_args: pytest.fail("short input must not commit")
    abi._release = lambda _header, buffer: released.append(buffer.token) or STATUS_OK
    abi._check = lambda status, _operation: assert_status_ok(status)
    monkeypatch.setattr(
        "rustyera_tui.abi.os.fstat",
        lambda _descriptor: SimpleNamespace(st_size=cache.stat().st_size + 1),
    )

    with pytest.raises(OSError, match="changed while being read"):
        abi.stage_compiled_cache_file(cache)

    assert released == [93]


@pytest.mark.parametrize("malformed", ["length", "pointer"])
def test_abi_35_rejects_malformed_writable_buffer_shapes(tmp_path: Path, malformed: str) -> None:
    cache = tmp_path / "compiled-project.reraproj"
    cache.write_bytes(b"cache")
    storage = (ctypes.c_uint8 * 5)()
    released: list[int] = []

    def allocate(_header: Any, _handle: Any, _length: int, output: Any) -> int:
        owned = ctypes.cast(output, ctypes.POINTER(EraOwnedBuffer)).contents
        owned.data = (
            ctypes.POINTER(ctypes.c_uint8)()
            if malformed == "pointer"
            else ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint8))
        )
        owned.len = 4 if malformed == "length" else 5
        owned.token = 94
        return STATUS_OK

    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._allocate_compiled_cache = allocate
    abi._commit_compiled_cache = lambda *_args: pytest.fail("malformed buffer must not commit")
    abi._release = lambda _header, buffer: released.append(buffer.token) or STATUS_OK
    abi._check = lambda status, _operation: assert_status_ok(status)

    with pytest.raises(AbiError, match="writable cache buffer"):
        abi.stage_compiled_cache_file(cache)

    assert released == [94]


@pytest.mark.parametrize(
    ("status", "released"),
    [(STATUS_BUSY, False), (STATUS_INVALID_ARGUMENT, True), (99, True)],
)
def test_abi_35_commit_status_has_explicit_consumption_rules(
    tmp_path: Path, status: int, released: bool
) -> None:
    cache = tmp_path / "compiled-project.reraproj"
    cache.write_bytes(b"cache")
    storage = (ctypes.c_uint8 * 5)()
    releases: list[int] = []

    def allocate(_header: Any, _handle: Any, length: int, output: Any) -> int:
        owned = ctypes.cast(output, ctypes.POINTER(EraOwnedBuffer)).contents
        owned.data = ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint8))
        owned.len = length
        owned.token = 95
        return STATUS_OK

    def check(actual: int, operation: str) -> None:
        if actual != STATUS_OK:
            raise AbiError(f"{operation}: {actual}")

    abi = RuntimeAbi.__new__(RuntimeAbi)
    abi.handle = EraSessionHandle(1)
    abi._allocate_compiled_cache = allocate
    abi._commit_compiled_cache = lambda *_args: status
    abi._release = lambda _header, buffer: releases.append(buffer.token) or STATUS_OK
    abi._check = check

    with pytest.raises(AbiError, match="session_commit_compiled_cache"):
        abi.stage_compiled_cache_file(cache)

    assert releases == ([95] if released else [])


def assert_status_ok(status: int) -> None:
    assert status == STATUS_OK
