"""Bounded text sizing and representation helpers."""

from __future__ import annotations

import reprlib
from collections.abc import Iterator

_UTF8_CHUNK_CHARACTERS = 4_096


def iter_utf8_chunks(text: str) -> Iterator[bytes]:
    """Yield fixed-character UTF-8 buffers for streaming consumers."""

    for offset in range(0, len(text), _UTF8_CHUNK_CHARACTERS):
        yield text[offset : offset + _UTF8_CHUNK_CHARACTERS].encode("utf-8")


def utf8_length(text: str, *, stop_after: int | None = None) -> int:
    """Count UTF-8 bytes without allocating one encoded copy of the whole string."""

    total = 0
    for chunk in iter_utf8_chunks(text):
        total += len(chunk)
        if stop_after is not None and total > stop_after:
            return total
    return total


def truncate_utf8(text: str, maximum_bytes: int, marker: str = "…") -> str:
    """Return a UTF-8 bounded prefix using only fixed-size temporary buffers."""

    if maximum_bytes <= 0:
        return ""
    if utf8_length(text, stop_after=maximum_bytes) <= maximum_bytes:
        return text
    marker_bytes = utf8_length(marker)
    if marker_bytes > maximum_bytes:
        marker = ""
        marker_bytes = 0
    remaining = maximum_bytes - marker_bytes
    pieces: list[str] = []
    offset = 0
    while offset < len(text) and remaining:
        chunk = text[offset : offset + _UTF8_CHUNK_CHARACTERS]
        encoded_length = len(chunk.encode("utf-8"))
        if encoded_length <= remaining:
            pieces.append(chunk)
            remaining -= encoded_length
            offset += len(chunk)
            continue
        low = 0
        high = len(chunk)
        while low < high:
            middle = (low + high + 1) // 2
            if len(chunk[:middle].encode("utf-8")) <= remaining:
                low = middle
            else:
                high = middle - 1
        if low:
            pieces.append(chunk[:low])
        break
    return "".join(pieces) + marker


def bounded_repr(value: object, maximum_bytes: int) -> str:
    """Produce a reprlib-style representation within a UTF-8 byte budget."""

    formatter = reprlib.Repr()
    approximate_characters = max(8, maximum_bytes // 2)
    formatter.maxstring = approximate_characters
    formatter.maxother = approximate_characters
    formatter.maxlong = approximate_characters
    formatter.maxlist = 8
    formatter.maxtuple = 8
    formatter.maxset = 8
    formatter.maxfrozenset = 8
    formatter.maxdict = 8
    formatter.maxdeque = 8
    formatter.maxarray = 8
    return truncate_utf8(formatter.repr(value), maximum_bytes)


def retained_text_utf8_length(value: object, *, stop_after: int | None = None) -> int:
    """Count retained textual payloads in a small object graph without rendering it."""

    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += utf8_length(
                item,
                stop_after=None if stop_after is None else max(0, stop_after - total),
            )
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
        if stop_after is not None and total > stop_after:
            return total
    return total
