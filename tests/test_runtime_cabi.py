from __future__ import annotations

import queue
import time
from pathlib import Path
from typing import Callable

import pytest

from rustyera_tui.abi import DEFAULT_MAXIMUM_VM_INSTRUCTIONS, AbiError, discover_library
from rustyera_tui.runtime import FrontendEvent, RuntimeWorker

try:
    RUNTIME_LIBRARY = discover_library()
except AbiError:
    RUNTIME_LIBRARY = None


def test_default_drive_budget_keeps_the_caller_pump_cooperative() -> None:
    assert DEFAULT_MAXIMUM_VM_INSTRUCTIONS == 100_000


def wait_for(
    worker: RuntimeWorker,
    predicate: Callable[[FrontendEvent], bool],
    timeout: float = 15,
) -> FrontendEvent:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = worker.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event.kind == "error":
            raise AssertionError(event.value)
        if predicate(event):
            return event
    raise AssertionError("timed out waiting for runtime event")


@pytest.mark.skipif(RUNTIME_LIBRARY is None, reason="era-runtime-capi has not been built")
def test_real_c_abi_loads_starts_and_serves_debug_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(tmp_path / "data"))
    project = Path(__file__).parent / "fixtures" / "minimal"
    worker = RuntimeWorker(RUNTIME_LIBRARY, project)
    worker.start()
    try:
        wait_for(worker, lambda event: event.kind == "project_loaded")
        wait = wait_for(worker, lambda event: event.kind == "wait" and event.value is not None)
        assert wait.value[1] == 0

        worker.send("reload_all")
        reloaded = wait_for(
            worker,
            lambda event: event.kind == "status" and "热重载完成" in str(event.value),
        )
        assert "完成" in reloaded.value

        worker.send("debug_enable")
        wait_for(worker, lambda event: event.kind == "debug_enabled" and event.value)
        worker.send("debug_action", ("variables", None))
        wait_for(worker, lambda event: event.kind == "debug_stopped")
        response = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "variables",
        )
        pending, response_tag, fields = response.value
        assert pending == "variables"
        assert response_tag == 1
        assert isinstance(fields[0].get(1), list)
        descriptor = fields[0][1][0]
        worker.send("debug_action", ("read_variable", descriptor))
        value = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "variable_value",
        )
        assert value.value[1] == 2

        worker.send("debug_action", ("fibers", None))
        fibers = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "fibers",
        ).value[2][0]
        assert fibers[1]
        fiber_id = fibers[1][0][0]
        worker.send("debug_action", ("call_stack", fiber_id))
        stack = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "call_stack",
        ).value[2][0]
        assert stack[2]
        worker.send("debug_action", ("operand_stack", (fiber_id, stack[2][0][0])))
        operands = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "operand_stack",
        )
        assert operands.value[1] == 7

        worker.send("debug_action", ("console_evaluate", "1 + 2"))
        console = wait_for(
            worker,
            lambda event: event.kind == "debug_response" and event.value[0] == "console",
        )
        assert console.value[1] == 8
    finally:
        worker.stop()
        worker.join(timeout=3)
