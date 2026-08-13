"""Private high-level C ABI project-file conversion operations."""

from __future__ import annotations

import ctypes
from typing import Any, Callable

from .wire import decode, encode


def stage_project_manifest(
    runtime: Any,
    manifest: dict[int, object],
    *,
    header: Callable[[int], Any],
    call_header_size: int,
    borrowed_bytes: Callable[[bytes], Any],
) -> bool:
    """Stage one encoded manifest only when the negotiated ABI exposes the entry point."""

    stage = getattr(runtime, "_stage_project_manifest", None)
    if stage is None:
        return False
    data = encode(manifest)
    status = stage(header(call_header_size), runtime.handle, borrowed_bytes(data))
    runtime._check(status, "session_stage_project_manifest")
    return True


def decode_project_manifest(
    runtime: Any,
    data: bytes,
    decoder: Any,
    *,
    owned_buffer_type: type[Any],
    header: Callable[[int], Any],
    borrowed_bytes: Callable[[bytes], Any],
    status_text: Callable[[int], str],
    abi_error: type[RuntimeError],
) -> dict[int, object]:
    """Decode and release a project-file manifest while preserving the native buffer lifetime."""

    if decoder is None:
        raise abi_error("runtime ABI does not support RustyEra project files")
    output = owned_buffer_type()
    call_header = header(ctypes.sizeof(owned_buffer_type))
    status = decoder(call_header, runtime.handle, borrowed_bytes(data), ctypes.byref(output))
    runtime._check(status, "session_decode_project_file")
    try:
        decoded = decode(ctypes.string_at(output.data, output.len))
    finally:
        release_status = runtime._release(call_header, output)
        if release_status != 0:
            raise abi_error(f"release_buffer failed with status {status_text(release_status)}")
    if not isinstance(decoded, dict):
        raise abi_error("runtime returned an invalid project-file manifest")
    return decoded


def prepare_project_configuration_update(
    runtime: Any,
    project_file: bytes,
    expected_digest: bytes,
    contents: str,
    *,
    owned_buffer_type: type[Any],
    byte_slice_type: type[Any],
    header: Callable[[int], Any],
    status_text: Callable[[int], str],
    abi_error: type[RuntimeError],
) -> tuple[int, bytes]:
    """Prepare and decode one append-only project configuration update through the C ABI."""

    prepare = runtime._prepare_project_configuration_update
    if prepare is None:
        raise abi_error("runtime ABI does not support writable RustyEra project files")
    contents_bytes = contents.encode("utf-8")
    project_buffer = ctypes.c_char_p(project_file)
    expected_buffer = ctypes.c_char_p(expected_digest)
    contents_buffer = ctypes.c_char_p(contents_bytes)
    output = owned_buffer_type()
    call_header = header(ctypes.sizeof(owned_buffer_type))
    status = prepare(
        call_header,
        runtime.handle,
        byte_slice_type(
            ctypes.cast(project_buffer, ctypes.POINTER(ctypes.c_uint8)), len(project_file)
        ),
        byte_slice_type(
            ctypes.cast(expected_buffer, ctypes.POINTER(ctypes.c_uint8)), len(expected_digest)
        ),
        byte_slice_type(
            ctypes.cast(contents_buffer, ctypes.POINTER(ctypes.c_uint8)), len(contents_bytes)
        ),
        ctypes.byref(output),
    )
    runtime._check(status, "prepare_project_configuration_update")
    try:
        encoded = ctypes.string_at(output.data, output.len)
    finally:
        release_status = runtime._release(call_header, output)
        if release_status != 0:
            raise abi_error(f"release_buffer failed with status {status_text(release_status)}")
    if len(encoded) < 8:
        raise abi_error("runtime returned an invalid project configuration update")
    return int.from_bytes(encoded[:8], "little"), encoded[8:]
