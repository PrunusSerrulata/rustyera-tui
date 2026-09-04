"""Bounded snake storage matching shared by Data and Resource listings."""

from __future__ import annotations

import unicodedata

MAXIMUM_PATTERN_BYTES = 4096
MAXIMUM_MATCH_STEPS = 1_048_576


def _normalized(value: str) -> str:
    if "\0" in value:
        raise ValueError("storage pattern or name contains NUL")
    if len(value.encode("utf-8")) > MAXIMUM_PATTERN_BYTES:
        raise ValueError("storage pattern or name exceeds the UTF-8 limit")
    normalized = unicodedata.normalize("NFC", value).lower()
    if len(normalized.encode("utf-8")) > MAXIMUM_PATTERN_BYTES:
        raise ValueError("normalized storage pattern or name exceeds the UTF-8 limit")
    return normalized


class SnakeStoragePattern:
    def __init__(self, pattern: str | None):
        # An absent or empty filter retains the existing match-all convention.
        self.pattern = _normalized(pattern) if pattern else None

    def matches(self, name: str) -> bool:
        name = _normalized(name)
        pattern = self.pattern
        if pattern is None:
            return True
        name_index = pattern_index = steps = 0
        star_index = -1
        star_name_index = 0
        # Only the latest '*' is reconsidered; no recursive/exponential regex search.
        # Python indexes Unicode scalars, so '?' consumes one non-BMP character too.
        while name_index < len(name):
            steps += 1
            if steps > MAXIMUM_MATCH_STEPS:
                raise ValueError("storage pattern matching exceeds the operation limit")
            if pattern_index < len(pattern) and (
                pattern[pattern_index] == "?"
                or (pattern[pattern_index] != "*" and pattern[pattern_index] == name[name_index])
            ):
                name_index += 1
                pattern_index += 1
            elif pattern_index < len(pattern) and pattern[pattern_index] == "*":
                star_index = pattern_index
                star_name_index = name_index
                pattern_index += 1
            elif star_index >= 0:
                star_name_index += 1
                name_index = star_name_index
                pattern_index = star_index + 1
            else:
                return False
        while pattern_index < len(pattern) and pattern[pattern_index] == "*":
            steps += 1
            if steps > MAXIMUM_MATCH_STEPS:
                raise ValueError("storage pattern matching exceeds the operation limit")
            pattern_index += 1
        return pattern_index == len(pattern)
