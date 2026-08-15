"""Opt-in startup milestone stream for out-of-process performance measurement."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from typing import Any

try:
    import resource
except ImportError:  # Windows does not provide the POSIX resource module.
    resource = None  # type: ignore[assignment]

STARTUP_TELEMETRY_FD_ENV = "RUSTYERA_STARTUP_TELEMETRY_FD"


def emit_startup_milestone(event: str, **fields: Any) -> None:
    """Write one compact event to an inherited descriptor when measurement is enabled."""

    raw_fd = os.environ.get(STARTUP_TELEMETRY_FD_ENV)
    if raw_fd is None or resource is None:
        return
    try:
        fd = int(raw_fd)
        if os.get_blocking(fd) or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_bytes = peak_rss if sys.platform == "darwin" else peak_rss * 1024
        payload = {
            "event": event,
            "client": "tui",
            "runtime_monotonic_ns": time.monotonic_ns(),
            "peak_rss_bytes": peak_rss_bytes,
            **fields,
        }
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        fpathconf = getattr(os, "fpathconf", None)
        try:
            pipe_buf = fpathconf(fd, "PC_PIPE_BUF") if fpathconf is not None else 512
        except (OSError, ValueError):
            pipe_buf = 512
        if len(encoded) > pipe_buf:
            return
        os.write(fd, encoded)
    except (OSError, TypeError, ValueError):
        # Telemetry must never alter startup behavior or turn a measurement failure into an
        # application failure.
        return
