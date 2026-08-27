"""Fixed protocol fixtures for identities returned by the current core resolver."""

from typing import Any


def reference_identity() -> dict[int, Any]:
    return {
        0: 0,
        1: 1,
        2: 1,
        3: "wrapping_i64_v1",
        4: "sfmt19937",
        5: 1,
        6: "unicode_column_v1",
        7: "emuera1808",
        8: [],
    }


def snake_identity() -> dict[int, Any]:
    return {**reference_identity(), 0: 1, 1: 2, 2: 2, 7: "rustyera_envelope_v1:emuera1808"}
