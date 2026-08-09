"""Checked ctypes projection of `era_runtime.h`."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Callable

from .protocol_text import ERA_STATUSES, enum_text
from .wire import decode

ABI_MAJOR = 3
ABI_MINOR = 6

STATUS_OK = 0
STATUS_EMPTY = 1
STATUS_BUSY = 2
STATUS_INVALID_ARGUMENT = 3
STATUS_ABI_MISMATCH = 4
STATUS_INVALID_HANDLE = 5
STATUS_RESOURCE_LIMIT = 6
STATUS_INTERNAL_ERROR = 7
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


class EraProjectProgress(ctypes.Structure):
    _fields_ = [
        ("header", EraCallHeader),
        ("stage", ctypes.c_uint32),
        ("completed", ctypes.c_uint64),
        ("total", ctypes.c_uint64),
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
ProjectProgressCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, EraProjectProgress)
SessionSetProjectProgress = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    ProjectProgressCallback,
    ctypes.c_void_p,
)
SessionDecodeProjectFile = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    EraByteSlice,
    ctypes.POINTER(EraOwnedBuffer),
)
SessionStageCompiledCache = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    EraByteSlice,
    ctypes.POINTER(ctypes.c_uint64),
)
SessionAllocateCompiledCache = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    ctypes.c_size_t,
    ctypes.POINTER(EraOwnedBuffer),
)
SessionCommitCompiledCache = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    EraOwnedBuffer,
    ctypes.POINTER(ctypes.c_uint64),
)
PrepareProjectConfigurationUpdate = ctypes.CFUNCTYPE(
    ctypes.c_uint32,
    EraCallHeader,
    EraSessionHandle,
    EraByteSlice,
    EraByteSlice,
    EraByteSlice,
    ctypes.POINTER(EraOwnedBuffer),
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
        project_progress: Callable[[int, int, int], None] | None = None,
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
        self._project_progress_handler = project_progress
        self._project_progress_callback = ProjectProgressCallback(self._on_project_progress)
        self._set_project_progress = (
            SessionSetProjectProgress(api.reserved[0])
            if api.abi_version.minor >= 1 and api.reserved[0]
            else None
        )
        self._decode_project_file = (
            SessionDecodeProjectFile(api.reserved[1])
            if api.abi_version.minor >= 2 and api.reserved[1]
            else None
        )
        self._decode_project_file_frontend = (
            SessionDecodeProjectFile(api.reserved[2])
            if api.abi_version.minor >= 3 and api.reserved[2]
            else None
        )
        self._stage_compiled_cache = (
            SessionStageCompiledCache(api.reserved[3])
            if api.abi_version.minor >= 4 and api.reserved[3]
            else None
        )
        self._allocate_compiled_cache = (
            SessionAllocateCompiledCache(api.reserved[4])
            if api.abi_version.minor >= 5 and api.reserved[4]
            else None
        )
        self._commit_compiled_cache = (
            SessionCommitCompiledCache(api.reserved[5])
            if api.abi_version.minor >= 5 and api.reserved[5]
            else None
        )
        self._prepare_project_configuration_update = (
            PrepareProjectConfigurationUpdate(api.reserved[6])
            if api.abi_version.minor >= 6 and api.reserved[6]
            else None
        )
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
        if self._set_project_progress is not None and self._project_progress_handler is not None:
            status = self._set_project_progress(
                _header(ctypes.sizeof(EraCallHeader)),
                self.handle,
                self._project_progress_callback,
                None,
            )
            self._check(status, "session_set_project_progress")

    def _on_project_progress(self, _context: ctypes.c_void_p, progress: EraProjectProgress) -> None:
        if self._project_progress_handler is not None:
            self._project_progress_handler(progress.stage, progress.completed, progress.total)

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
                raise AbiError(f"release_buffer failed with status {_status_text(release_status)}")

    def project_file_manifest(self, data: bytes) -> dict[int, object]:
        """Decode a validated project-file manifest through the core ABI."""

        decoder = getattr(self, "_decode_project_file_frontend", None) or self._decode_project_file
        if decoder is None:
            raise AbiError("runtime ABI does not support RustyEra project files")
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        output = EraOwnedBuffer()
        header = _header(ctypes.sizeof(EraOwnedBuffer))
        status = decoder(
            header,
            self.handle,
            EraByteSlice(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)), len(data)),
            ctypes.byref(output),
        )
        self._check(status, "session_decode_project_file")
        try:
            decoded = decode(ctypes.string_at(output.data, output.len))
        finally:
            release_status = self._release(header, output)
            if release_status != STATUS_OK:
                raise AbiError(f"release_buffer failed with status {_status_text(release_status)}")
        if not isinstance(decoded, dict):
            raise AbiError("runtime returned an invalid project-file manifest")
        return decoded

    @property
    def supports_project_configuration_updates(self) -> bool:
        """Return whether the loaded runtime can prepare append-only project edits."""

        return self._prepare_project_configuration_update is not None

    def prepare_project_configuration_update(
        self, project_file: bytes, expected_digest: bytes, contents: str
    ) -> tuple[int, bytes]:
        """Validate a package and return its truncation offset and compact append record."""

        prepare = self._prepare_project_configuration_update
        if prepare is None:
            raise AbiError("runtime ABI does not support writable RustyEra project files")
        contents_bytes = contents.encode("utf-8")
        project_buffer = ctypes.c_char_p(project_file)
        expected_buffer = ctypes.c_char_p(expected_digest)
        contents_buffer = ctypes.c_char_p(contents_bytes)
        output = EraOwnedBuffer()
        header = _header(ctypes.sizeof(EraOwnedBuffer))
        status = prepare(
            header,
            self.handle,
            EraByteSlice(
                ctypes.cast(project_buffer, ctypes.POINTER(ctypes.c_uint8)), len(project_file)
            ),
            EraByteSlice(
                ctypes.cast(expected_buffer, ctypes.POINTER(ctypes.c_uint8)), len(expected_digest)
            ),
            EraByteSlice(
                ctypes.cast(contents_buffer, ctypes.POINTER(ctypes.c_uint8)), len(contents_bytes)
            ),
            ctypes.byref(output),
        )
        self._check(status, "prepare_project_configuration_update")
        try:
            encoded = ctypes.string_at(output.data, output.len)
        finally:
            release_status = self._release(header, output)
            if release_status != STATUS_OK:
                raise AbiError(f"release_buffer failed with status {_status_text(release_status)}")
        if len(encoded) < 8:
            raise AbiError("runtime returned an invalid project configuration update")
        return int.from_bytes(encoded[:8], "little"), encoded[8:]

    def stage_compiled_cache(self, data: bytes) -> int | None:
        """Stage a contiguous cache without encoding protocol chunks when ABI 3.4 is available."""

        stage = getattr(self, "_stage_compiled_cache", None)
        if stage is None:
            return None
        # c_char_p retains the immutable bytes object for this synchronous call, avoiding a
        # second Python-side copy before the C boundary makes its required owned copy.
        buffer = ctypes.c_char_p(data)
        transfer_id = ctypes.c_uint64()
        status = stage(
            _header(ctypes.sizeof(ctypes.c_uint64)),
            self.handle,
            EraByteSlice(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)), len(data)),
            ctypes.byref(transfer_id),
        )
        self._check(status, "session_stage_compiled_cache")
        if transfer_id.value == 0:
            raise AbiError("runtime returned an invalid compiled-cache transfer ID")
        return transfer_id.value

    def stage_compiled_cache_file(self, path: Path) -> int | None:
        """Read a cache directly into Runtime-owned memory when ABI 3.5 is available."""

        allocate = getattr(self, "_allocate_compiled_cache", None)
        commit = getattr(self, "_commit_compiled_cache", None)
        if allocate is None or commit is None:
            return None
        header = _header(ctypes.sizeof(EraOwnedBuffer))
        output = EraOwnedBuffer()
        with path.open("rb", buffering=0) as stream:
            length = os.fstat(stream.fileno()).st_size
            status = allocate(header, self.handle, length, ctypes.byref(output))
            self._check(status, "session_allocate_compiled_cache")
            committed = False
            try:
                if output.len != length:
                    raise AbiError(
                        "runtime returned a writable cache buffer with an unexpected length"
                    )
                if length:
                    address = ctypes.cast(output.data, ctypes.c_void_p).value
                    if address is None:
                        raise AbiError("runtime returned a null writable cache buffer")
                    array = (ctypes.c_uint8 * length).from_address(address)
                    target = memoryview(array).cast("B")
                    offset = 0
                    while offset < length:
                        read = stream.readinto(target[offset:])
                        if not read:
                            raise OSError("compiled project cache changed while being read")
                        offset += read
                    if stream.read(1):
                        raise OSError("compiled project cache changed while being read")
                transfer_id = ctypes.c_uint64()
                status = commit(
                    _header(ctypes.sizeof(ctypes.c_uint64)),
                    self.handle,
                    output,
                    ctypes.byref(transfer_id),
                )
                # Shape/handle/header rejection leaves ownership with the caller. Once those
                # checks pass, Runtime consumes the allocation even when staging itself fails.
                committed = status in {
                    STATUS_OK,
                    STATUS_BUSY,
                    STATUS_RESOURCE_LIMIT,
                    STATUS_INTERNAL_ERROR,
                }
                self._check(status, "session_commit_compiled_cache")
                if transfer_id.value == 0:
                    raise AbiError("runtime returned an invalid compiled-cache transfer ID")
                return transfer_id.value
            finally:
                if not committed:
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
