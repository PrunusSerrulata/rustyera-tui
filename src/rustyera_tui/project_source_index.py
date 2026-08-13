"""Path normalization and durable source-index helpers for project scanning."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


def _normalize_relative_path(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def _path_sort_key(path: str) -> tuple[str, str]:
    """Match the runtime's locale-independent lowercase/path ordering."""

    return path.lower(), path


def _normalize_resource_manifest_paths(text: str) -> str:
    normalized: list[str] = []
    for body, ending in _resource_manifest_lines(text):
        fields = body.split(",")
        if len(fields) >= 2:
            value = fields[1]
            stripped = value.strip(" \t")
            if stripped and stripped.lower() != "anime":
                leading = value[: len(value) - len(value.lstrip(" \t"))]
                trailing = value[len(value.rstrip(" \t")) :]
                fields[1] = f"{leading}{unicodedata.normalize('NFC', stripped)}{trailing}"
        normalized.append(",".join(fields) + ending)
    return "".join(normalized)


def _resource_manifest_lines(text: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    start = 0
    while start < len(text):
        cr = text.find("\r", start)
        lf = text.find("\n", start)
        endings = [offset for offset in (cr, lf) if offset >= 0]
        if not endings:
            lines.append((text[start:], ""))
            break
        ending_start = min(endings)
        ending_end = ending_start + (2 if text.startswith("\r\n", ending_start) else 1)
        lines.append((text[start:ending_start], text[ending_start:ending_end]))
        start = ending_end
    return lines


def _source_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        getattr(stat, "st_dev", 0),
        getattr(stat, "st_ino", 0),
    )


def _write_source_index(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        native_contents = contents.replace("\r\n", "\n").replace("\r", "\n")
        native_contents = native_contents.replace("\n", os.linesep)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(native_contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
