"""NDJSON trace persistence and bounded stdout projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

STREAM_OUTPUT_LINES = 30


class TraceWriter:
    def __init__(self, path: Path, stream: TextIO):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")
        self.stream = stream

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.file.write(line + "\n")
        self.file.flush()
        stream_event = dict(event)
        if event.get("type") == "observation":
            for implementation in ("rust", "reference"):
                observation = event.get(implementation)
                if isinstance(observation, dict):
                    streamed_observation = {
                        key: value for key, value in observation.items() if key != "output"
                    }
                    delta = observation.get("output_delta")
                    if isinstance(delta, dict) and isinstance(delta.get("added"), list):
                        added = delta["added"]
                        if len(added) > STREAM_OUTPUT_LINES:
                            streamed_observation["output_delta"] = {
                                **delta,
                                "added": added[-STREAM_OUTPUT_LINES:],
                                "added_omitted": len(added) - STREAM_OUTPUT_LINES,
                            }
                    stream_event[implementation] = streamed_observation
        self.stream.write(
            json.dumps(stream_event, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.stream.flush()

    def close(self) -> None:
        self.file.close()
