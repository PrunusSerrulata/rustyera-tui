"""Persistent Emuera reference process adapter for scenario comparisons."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from .testing_support import TestDriverError, output_delta

REFERENCE_SCHEMA_VERSION = 2


class ReferenceProcess:
    def __init__(
        self,
        command: list[str],
        path_converter: list[str] | None = None,
        timeout_seconds: float = 30,
        cwd: Path | None = None,
    ):
        self.path_converter = path_converter
        self.timeout_seconds = timeout_seconds
        self.process = subprocess.Popen(  # noqa: S603 - explicit scenario-owned command
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=cwd,
        )
        self.next_id = 1
        self.schema_version: int | None = None
        self.reference_commit: str | None = None
        self.previous_output: list[str] = []
        self.responses: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_responses, daemon=True)
        self.reader.start()

    def _read_responses(self) -> None:
        if not self.process.stdout:
            self.responses.put(None)
            return
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()

    def convert_path(self, path: Path) -> str:
        if not self.path_converter:
            return str(path)
        completed = subprocess.run(  # noqa: S603 - explicit scenario-owned command
            [*self.path_converter, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        if not self.process.stdin:
            raise TestDriverError("reference process pipes are unavailable")
        request = {"id": self.next_id, "op": operation, **fields}
        self.next_id += 1
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            line = self.responses.get(timeout=self.timeout_seconds)
        except queue.Empty as error:
            raise TestDriverError("reference process timed out") from error
        if not line:
            detail = self.process.stderr.read() if self.process.stderr else ""
            raise TestDriverError(f"reference process stopped without a response: {detail}")
        response = json.loads(line)
        if not response.get("ok"):
            raise TestDriverError(f"reference request failed: {response.get('error')}")
        schema_version = response.get("schemaVersion")
        if schema_version != REFERENCE_SCHEMA_VERSION:
            raise TestDriverError(
                f"reference schema {schema_version!r} is not {REFERENCE_SCHEMA_VERSION}"
            )
        if response.get("id") != request["id"]:
            raise TestDriverError("reference response id does not match its request")
        self.schema_version = schema_version
        self.reference_commit = response.get("referenceCommit")
        return response["result"]

    def start(self, scenario: Any, project_override: Path | None = None) -> dict[str, Any]:
        capabilities = self.request("capabilities")
        operations = set(capabilities.get("operations", []))
        required = {"load", "run"}
        if scenario.start.type == "traditional_save":
            required.add("loadSave")
        missing = sorted(required - operations)
        if missing:
            raise TestDriverError(f"reference CLI is missing operations: {', '.join(missing)}")
        result = self.request(
            "load",
            gameDir=self.convert_path(project_override or scenario.project),
            seed=scenario.seed,
            watch=list(scenario.watches),
        )
        if scenario.start.type == "traditional_save" and scenario.start.path is not None:
            result = self.request(
                "loadSave",
                savePath=self.convert_path(scenario.start.path),
                watch=list(scenario.watches),
            )
        return self.observe(result)

    def step(self, value: str, watches: tuple[str, ...]) -> dict[str, Any]:
        return self.observe(self.request("run", inputs=[value], watch=list(watches)))

    def observe(self, result: dict[str, Any]) -> dict[str, Any]:
        current = [str(item) for item in result.get("output", [])]
        delta = output_delta(self.previous_output, current)
        self.previous_output = current
        request = result.get("inputRequest") or {}
        return {
            "termination": result.get("termination"),
            "wait": {
                "kind": request.get("InputType"),
                "system_input": request.get("IsSystemInput", False),
            },
            "output": current,
            "output_delta": delta.as_dict(),
            "output_tail": current[-30:],
            "watches": result.get("watches", {}),
            "random_seed": result.get("randomSeed"),
            "random_algorithm": result.get("randomAlgorithm"),
            "schema_version": self.schema_version,
            "reference_commit": self.reference_commit,
        }
