"""Checked ctypes projection of `era_runtime.h`."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from .protocol_text import ERA_STATUSES, enum_text

ABI_MAJOR = 3
ABI_MINOR = 0

STATUS_OK = 0
STATUS_EMPTY = 1
DEFAULT_MAXIMUM_VM_INSTRUCTIONS = 100_000


def _status_text(status: int) -> str:
    return enum_text(status, ERA_STATUSES, "EraStatus")


class AbiError(RuntimeError):
    """A C ABI call failed before a protocol response could be produced."""


class EraAbiVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint16), ("minor", ctypes.c_uint16)]


class EraCallHeader(ctypes.Structure):
    _fields_ = [("struct_size", ctypes.c_uint32), ("abi_version", EraAbiVersion)]


class EraSessionHandle(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint64)]


class EraByteSlice(ctypes.Structure):
    _fields_ = [("data", ctypes.POINTER(ctypes.c_uint8)), ("len", ctypes.c_size_t)]


class EraOwnedBuffer(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("len", ctypes.c_size_t),
        ("token", ctypes.c_uint64),
    ]


class EraCreateOptions(ctypes.Structure):
    _fields_ = [
        ("header", EraCallHeader),
        ("debug_scope_mask", ctypes.c_uint64),
        ("reserved", ctypes.c_uint64 * 4),
    ]


class EraDriveOptions(ctypes.Structure):
    _fields_ = [
        ("header", EraCallHeader),
        ("maximum_vm_instructions", ctypes.c_uint64),
        ("maximum_runtime_transitions", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class EraDriveResult(ctypes.Structure):
    _fields_ = [
        ("header", EraCallHeader),
        ("state", ctypes.c_uint32),
        ("vm_instructions", ctypes.c_uint64),
        ("runtime_transitions", ctypes.c_uint32),
        ("queued_envelopes", ctypes.c_uint32),
    ]


SessionCreate = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    ctypes.POINTER(EraCreateOptions),
    ctypes.POINTER(EraSessionHandle),
)
SessionSubmit = ctypes.CFUNCTYPE(ctypes.c_uint32, EraCallHeader, EraSessionHandle, EraByteSlice)
SessionDrive = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    ctypes.POINTER(EraDriveOptions),
    ctypes.POINTER(EraDriveResult),
)
SessionPoll = ctypes.CFUNCTYPE(
    ctypes.c_uint32, EraCallHeader, EraSessionHandle, ctypes.POINTER(EraOwnedBuffer)
)
SessionDestroy = ctypes.CFUNCTYPE(ctypes.c_uint32, EraCallHeader, EraSessionHandle)
ReleaseBuffer = ctypes.CFUNCTYPE(ctypes.c_uint32, EraCallHeader, EraOwnedBuffer)
LastError = ctypes.CFUNCTYPE(
    ctypes.c_uint32, EraCallHeader, EraSessionHandle, ctypes.POINTER(EraOwnedBuffer)
)


class EraRuntimeApi(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", EraAbiVersion),
        ("implementation_name", ctypes.c_char_p),
        ("implementation_context", ctypes.c_void_p),
        ("session_create", ctypes.c_void_p),
        ("session_submit", ctypes.c_void_p),
        ("session_drive", ctypes.c_void_p),
        ("session_poll", ctypes.c_void_p),
        ("session_destroy", ctypes.c_void_p),
        ("release_buffer", ctypes.c_void_p),
        ("last_error", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p * 8),
    ]


def _header(size: int) -> EraCallHeader:
    return EraCallHeader(size, EraAbiVersion(ABI_MAJOR, ABI_MINOR))


def discover_library(
    explicit: Path | None = None,
    resource_directory: Path | None = None,
) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    if configured := os.environ.get("ERA_RUNTIME_LIBRARY"):
        return Path(configured).expanduser().resolve()
    suffix = {"darwin": ".dylib", "win32": ".dll"}.get(sys.platform, ".so")
    prefix = "" if sys.platform == "win32" else "lib"
    filename = f"{prefix}era_runtime_capi{suffix}"
    resource_root = (resource_directory or Path.cwd()).expanduser().resolve()
    resource_candidate = resource_root / filename
    if resource_candidate.is_file():
        return resource_candidate
    package_candidate = Path(__file__).resolve().parent / filename
    if package_candidate.is_file():
        return package_candidate
    raise AbiError(
        "era-runtime-capi dynamic library was not found at "
        f"{package_candidate} or {resource_candidate}; "
        "build and link the release library there or pass --runtime-library"
    )


class RuntimeAbi:
    """Own one dynamically loaded C API table and one optional session."""

    def __init__(
        self,
        library_path: Path | None = None,
        debug_scope_mask: int = (1 << 10) - 1,
        resource_directory: Path | None = None,
    ):
        self.path = discover_library(library_path, resource_directory)
        self.library = ctypes.CDLL(str(self.path))
        get_api = self.library.era_runtime_get_api
        get_api.argtypes = [EraAbiVersion, ctypes.POINTER(EraRuntimeApi)]
        get_api.restype = ctypes.c_uint32
        api = EraRuntimeApi()
        status = get_api(EraAbiVersion(ABI_MAJOR, ABI_MINOR), ctypes.byref(api))
        if status != STATUS_OK:
            raise AbiError(f"era_runtime_get_api failed with status {_status_text(status)}")
        if api.abi_version.major != ABI_MAJOR:
            raise AbiError(f"runtime returned incompatible ABI {api.abi_version.major}")
        self.api = api
        self._create = SessionCreate(api.session_create)
        self._submit = SessionSubmit(api.session_submit)
        self._drive = SessionDrive(api.session_drive)
        self._poll = SessionPoll(api.session_poll)
        self._destroy = SessionDestroy(api.session_destroy)
        self._release = ReleaseBuffer(api.release_buffer)
        self._last_error = LastError(api.last_error)
        self.debug_scope_mask = debug_scope_mask
        self.handle = EraSessionHandle()
        self.create_session()

    def create_session(self) -> None:
        if self.handle.value:
            raise AbiError("a runtime session is already active")
        options = EraCreateOptions(
            _header(ctypes.sizeof(EraCreateOptions)),
            self.debug_scope_mask,
            (ctypes.c_uint64 * 4)(0, 0, 0, 0),
        )
        handle = EraSessionHandle()
        status = self._create(options.header, ctypes.byref(options), ctypes.byref(handle))
        self._check(status, "session_create", handle=False)
        self.handle = handle

    def recreate_session(self) -> None:
        self.destroy_session()
        self.create_session()

    def submit(self, data: bytes) -> None:
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))
        status = self._submit(
            _header(ctypes.sizeof(EraCallHeader)),
            self.handle,
            EraByteSlice(pointer, len(data)),
        )
        self._check(status, "session_submit")

    def drive(self, maximum_instructions: int = DEFAULT_MAXIMUM_VM_INSTRUCTIONS) -> EraDriveResult:
        options = EraDriveOptions(
            _header(ctypes.sizeof(EraDriveOptions)), maximum_instructions, 1024, 0
        )
        result = EraDriveResult()
        status = self._drive(
            options.header, self.handle, ctypes.byref(options), ctypes.byref(result)
        )
        self._check(status, "session_drive")
        return result

    def poll(self) -> bytes | None:
        output = EraOwnedBuffer()
        header = _header(ctypes.sizeof(EraOwnedBuffer))
        status = self._poll(header, self.handle, ctypes.byref(output))
        if status == STATUS_EMPTY:
            return None
        self._check(status, "session_poll")
        try:
            return ctypes.string_at(output.data, output.len)
        finally:
            release_status = self._release(header, output)
            if release_status != STATUS_OK:
                raise AbiError(
                    f"release_buffer failed with status {_status_text(release_status)}"
                )

    def last_error(self) -> str:
        if not self.handle.value:
            return ""
        output = EraOwnedBuffer()
        header = _header(ctypes.sizeof(EraOwnedBuffer))
        status = self._last_error(header, self.handle, ctypes.byref(output))
        if status != STATUS_OK:
            return f"last_error failed with status {_status_text(status)}"
        try:
            return ctypes.string_at(output.data, output.len).decode("utf-8", "replace")
        finally:
            self._release(header, output)

    def destroy_session(self) -> None:
        if not self.handle.value:
            return
        status = self._destroy(_header(ctypes.sizeof(EraCallHeader)), self.handle)
        if status != STATUS_OK:
            raise AbiError(f"session_destroy failed with status {_status_text(status)}")
        self.handle = EraSessionHandle()

    def close(self) -> None:
        self.destroy_session()

    def _check(self, status: int, operation: str, *, handle: bool = True) -> None:
        if status == STATUS_OK:
            return
        detail = self.last_error() if handle and self.handle.value else ""
        suffix = f": {detail}" if detail else ""
        raise AbiError(f"{operation} failed with status {_status_text(status)}{suffix}")

    def __enter__(self) -> RuntimeAbi:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
