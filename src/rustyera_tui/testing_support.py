"""Shared observation normalization for runtime and reference test drivers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class TestDriverError(RuntimeError):
    """A scenario, runtime, or reference process could not be driven safely."""

    __test__ = False


@dataclass(frozen=True, slots=True)
class OutputDelta:
    reset: bool
    removed: int
    added: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"reset": self.reset, "removed": self.removed, "added": list(self.added)}


def normalized_lines(lines: list[str], ignore: list[str] | None = None) -> list[str]:
    patterns = [re.compile(pattern) for pattern in (ignore or [])]
    result: list[str] = []
    for line in lines:
        value = line.replace("\r", "").rstrip()
        if any(pattern.search(value) for pattern in patterns):
            continue
        result.append(value)
    return result


def output_delta(previous: list[str], current: list[str]) -> OutputDelta:
    common = 0
    for left, right in zip(previous, current, strict=False):
        if left != right:
            break
        common += 1
    removed = len(previous) - common
    return OutputDelta(common == 0 and bool(previous), removed, tuple(current[common:]))
