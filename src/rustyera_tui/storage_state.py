"""Pure revision and optimistic-precondition values for frontend storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import blake3

from .frontend_io import IO_CONFLICT
from .wire import variant


def _revision(path: Path) -> str | None:
    try:
        hasher = blake3.blake3()
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()
    except FileNotFoundError:
        return None


def _change_token(stat: os.stat_result) -> str:
    return ":".join(
        str(value)
        for value in (
            getattr(stat, "st_dev", 0),
            getattr(stat, "st_ino", 0),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    )


def _precondition_conflict(path: Path, precondition: list[Any]) -> list[Any] | None:
    tag, fields = precondition
    revision = _revision(path)
    conflict = tag == 1 and revision is not None
    conflict = conflict or (tag == 2 and (not fields or revision != fields[0]))
    if not conflict:
        return None
    error = {0: IO_CONFLICT, 1: "storage precondition did not hold"}
    return variant(4, error)
