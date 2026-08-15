from __future__ import annotations

import json
import os

import rustyera_tui.startup_telemetry as startup_telemetry
from rustyera_tui.startup_telemetry import (
    STARTUP_TELEMETRY_FD_ENV,
    emit_startup_milestone,
)


def test_missing_posix_resource_module_disables_telemetry(monkeypatch) -> None:
    monkeypatch.setattr(startup_telemetry, "resource", None)
    monkeypatch.setenv(STARTUP_TELEMETRY_FD_ENV, "not-a-descriptor")

    emit_startup_milestone("attempt_started", attempt_id=1)


def test_emits_compact_startup_milestone_to_inherited_descriptor(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    monkeypatch.setenv(STARTUP_TELEMETRY_FD_ENV, str(write_fd))
    try:
        emit_startup_milestone("validation_complete", attempt_id=3, cache_hit=True)
        message = json.loads(os.read(read_fd, 4096))
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert message["event"] == "validation_complete"
    assert message["client"] == "tui"
    assert message["attempt_id"] == 3
    assert message["cache_hit"] is True
    assert isinstance(message["runtime_monotonic_ns"], int)
    assert isinstance(message["peak_rss_bytes"], int)
    assert message["peak_rss_bytes"] > 0


def test_invalid_telemetry_descriptor_never_breaks_startup(monkeypatch) -> None:
    monkeypatch.setenv(STARTUP_TELEMETRY_FD_ENV, "not-an-integer")
    emit_startup_milestone("attempt_started", attempt_id=1)


def test_blocking_descriptor_is_ignored_without_changing_its_state(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(STARTUP_TELEMETRY_FD_ENV, str(write_fd))
    try:
        emit_startup_milestone("attempt_started", attempt_id=1)
        assert os.get_blocking(write_fd) is True
        os.set_blocking(read_fd, False)
        try:
            assert os.read(read_fd, 1) == b""
        except BlockingIOError:
            pass
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_full_nonblocking_pipe_drops_whole_milestone(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    monkeypatch.setenv(STARTUP_TELEMETRY_FD_ENV, str(write_fd))
    try:
        while True:
            try:
                os.write(write_fd, b"x" * 512)
            except BlockingIOError:
                break
        emit_startup_milestone("must_be_dropped", attempt_id=9)
        chunks = []
        while True:
            try:
                chunks.append(os.read(read_fd, 64 * 1024))
            except BlockingIOError:
                break
        assert b"must_be_dropped" not in b"".join(chunks)
    finally:
        os.close(read_fd)
        os.close(write_fd)
