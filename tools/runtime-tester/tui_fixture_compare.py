"""Print the Rust C ABI projection for the reference CLI's load fixture."""

from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path

from audit_paths import REPOSITORY_ROOT, runtime_library

ROOT = REPOSITORY_ROOT
sys.path.insert(0, str(ROOT / "frontends" / "era-tui" / "src"))

from rustyera_tui.presentation import PresentationModel  # noqa: E402
from rustyera_tui.runtime import FrontendEvent, PresentationBatch, RuntimeWorker  # noqa: E402


def presentation_result(
    model: PresentationModel, event: FrontendEvent
) -> dict[str, object] | None:
    if event.kind != "presentation_batch":
        return None
    batch = event.value
    if not isinstance(batch, PresentationBatch):
        raise TypeError("worker returned an invalid presentation batch")
    if batch.snapshot is not None:
        model.apply_snapshot(batch.snapshot)
    if batch.delta is not None:
        model.apply_delta(batch.delta)
    if batch.active_wait is None:
        return None
    return {
        "termination": "waitingInput",
        "output": [
            "".join(segment.text for segment in line.segments) for line in model.lines
        ],
        "wait_kind": batch.active_wait[1],
        "system_input": batch.active_wait[5],
    }


def main() -> int:
    source = (
        ROOT / "reference" / "emuera.em" / "emuera-reference-cli" / "tests" / "fixture"
    )
    with tempfile.TemporaryDirectory(prefix="rustyera-fixture-compare-") as directory:
        fixture = Path(directory) / "fixture"
        shutil.copytree(source, fixture)
        worker = RuntimeWorker(runtime_library(), fixture)
        model = PresentationModel()
        worker.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    event = worker.events.get(timeout=0.25)
                except queue.Empty:
                    continue
                result = presentation_result(model, event)
                if result is not None:
                    print(json.dumps(result, ensure_ascii=False))
                    return 0
                if event.kind in ("error", "runtime_error"):
                    print(json.dumps({"error": event.value}, ensure_ascii=False))
                    return 1
            return 2
        finally:
            worker.stop()
            worker.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
