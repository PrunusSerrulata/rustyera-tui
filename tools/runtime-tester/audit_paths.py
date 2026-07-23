"""Shared repository and runtime-library path resolution for audit scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TOOL_ROOT.parents[1]


def project_path() -> Path:
    """Return the submitted game tree without scanning it inside the Rust runtime."""

    return Path(os.environ.get("ERA_AUDIT_PROJECT", REPOSITORY_ROOT / "reference" / "eraTW"))


def runtime_library() -> Path:
    """Resolve the release C ABI library, with an override for non-default builds."""

    override = os.environ.get("ERA_RUNTIME_CAPI")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        filename = "libera_runtime_capi.dylib"
    elif sys.platform == "win32":
        filename = "era_runtime_capi.dll"
    else:
        filename = "libera_runtime_capi.so"
    return REPOSITORY_ROOT / "target" / "release" / filename
