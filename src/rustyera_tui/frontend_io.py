"""Frontend I/O error projection shared by project and storage boundaries."""

from __future__ import annotations

from typing import Any

IO_NOT_FOUND = 0
IO_PERMISSION_DENIED = 1
IO_INVALID_DATA = 2
IO_INTERRUPTED = 3
IO_READ_ONLY = 4
IO_ALREADY_EXISTS = 5
IO_OTHER = 6
IO_CONFLICT = 7


def frontend_error(error: OSError | UnicodeError, kind: int | None = None) -> dict[int, Any]:
    if kind is None:
        if isinstance(error, FileNotFoundError):
            kind = IO_NOT_FOUND
        elif isinstance(error, PermissionError):
            kind = IO_PERMISSION_DENIED
        elif isinstance(error, UnicodeError):
            kind = IO_INVALID_DATA
        elif isinstance(error, InterruptedError):
            kind = IO_INTERRUPTED
        elif isinstance(error, FileExistsError):
            kind = IO_ALREADY_EXISTS
        else:
            kind = IO_OTHER
    platform_code = error.errno if isinstance(error, OSError) else None
    result: dict[int, Any] = {0: kind, 1: str(error)}
    if platform_code is not None:
        result[2] = platform_code
    return result
