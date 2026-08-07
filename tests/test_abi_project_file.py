from __future__ import annotations

import ctypes
from typing import Any

from rustyera_tui.abi import EraOwnedBuffer, EraSessionHandle, RuntimeAbi, STATUS_OK
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
