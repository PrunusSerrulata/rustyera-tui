"""Print the Rust C ABI projection for the reference CLI's load fixture."""

from __future__ import annotations

import json
import queue
import sys
import time

from audit_paths import REPOSITORY_ROOT, runtime_library

ROOT = REPOSITORY_ROOT
sys.path.insert(0, str(ROOT / "frontends" / "era-tui" / "src"))

from rustyera_tui.presentation import PresentationModel  # noqa: E402
from rustyera_tui.runtime import RuntimeWorker  # noqa: E402


def main() -> int:
    worker = RuntimeWorker(
        runtime_library(),
        ROOT / "reference" / "emuera.em" / "emuera-reference-cli" / "tests" / "fixture",
    )
    model = PresentationModel()
    worker.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                event = worker.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if event.kind == "presentation_snapshot":
                model.apply_snapshot(event.value)
            elif event.kind == "presentation_delta":
                model.apply_delta(event.value)
            elif event.kind in ("error", "runtime_error"):
                print(json.dumps({"error": event.value}, ensure_ascii=False))
                return 1
            elif event.kind == "wait" and event.value is not None:
                output = [
                    "".join(segment.text for segment in line.segments)
                    for line in model.lines
                ]
                print(
                    json.dumps(
                        {
                            "termination": "waitingInput",
                            "output": output,
                            "wait_kind": event.value[1],
                            "system_input": event.value[5],
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
        return 2
    finally:
        worker.stop()
        worker.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
