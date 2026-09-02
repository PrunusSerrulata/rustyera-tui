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
    return {
        **reference_identity(),
        0: 1,
        1: 12,
        2: 12,
        3: "snake_saturating_i64_v1",
        7: "snake_emuera1808_interop_v1",
        8: [
            {0: "rustyera.sql", 1: 1},
            {0: "rustyera.sql.limits", 1: 1},
            {0: "rustyera.scene", 1: 1},
            {0: "rustyera.audio", 1: 1},
        ],
    }
