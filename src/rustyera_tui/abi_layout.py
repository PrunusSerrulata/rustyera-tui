"""Private ctypes layouts re-exported by :mod:`rustyera_tui.abi`."""

import ctypes


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
