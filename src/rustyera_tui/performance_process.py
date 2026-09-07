"""Fail-closed ownership and identity checks for headless audit child processes."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence, TextIO


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    pgid: int
    started: str
    command: str

    @classmethod
    def capture(cls, pid: int) -> ProcessIdentity:
        if pid <= 0:
            raise ValueError("audit PID must be positive")

        def field(name: str) -> str:
            completed = subprocess.run(
                ("/bin/ps", "-o", f"{name}=", "-p", str(pid)),
                check=True,
                capture_output=True,
                text=True,
            )
            value = completed.stdout.strip()
            if not value:
                raise RuntimeError(f"could not establish {name} identity for PID {pid}")
            return value

        return cls(pid, int(field("pgid")), field("lstart"), field("command"))

    def assert_alive(self) -> None:
        current = self.capture(self.pid)
        if current != self:
            raise RuntimeError(f"PID {self.pid} no longer has the registered audit identity")


@dataclass(slots=True)
class OwnedProcess:
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProfilerCommand:
    argv: tuple[str, ...]
    output: Path
    tool_writes_output: bool = False


class OwnedProcessRegistry:
    """Own exact child process groups and never signal a recycled/unrelated PID."""

    def __init__(self) -> None:
        self._children: dict[int, OwnedProcess] = {}

    @property
    def manifest(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "pid": item.identity.pid,
                "pgid": item.identity.pgid,
                "started": item.identity.started,
                "command": item.identity.command,
                "argv": item.argv,
            }
            for item in self._children.values()
        )

    def spawn(
        self,
        argv: Sequence[str],
        *,
        allocation_round: bool = False,
        stdout: int | BinaryIO | None = None,
        stderr: int | BinaryIO | None = None,
        stdin: int | BinaryIO | None = None,
        pass_fds: tuple[int, ...] = (),
        environment: Mapping[str, str] | None = None,
    ) -> OwnedProcess:
        env = dict(os.environ if environment is None else environment)
        if allocation_round:
            # Native malloc stack logging must exist before the audited process starts.
            env["MallocStackLogging"] = "1"
        else:
            env.pop("MallocStackLogging", None)
        process = subprocess.Popen(
            tuple(argv),
            start_new_session=True,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
            env=env,
        )
        try:
            identity = ProcessIdentity.capture(process.pid)
            if identity.pgid != process.pid:
                raise RuntimeError("audit child did not create its own process group")
        except BaseException:
            process.terminate()
            process.wait()
            raise
        owned = OwnedProcess(process, identity, tuple(argv))
        self._children[process.pid] = owned
        return owned

    @staticmethod
    def descendants(root_pid: int) -> tuple[ProcessIdentity, ...]:
        completed = subprocess.run(
            ("/bin/ps", "-axo", "pid=,ppid="),
            check=True,
            capture_output=True,
            text=True,
        )
        children: dict[int, list[int]] = {}
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2:
                children.setdefault(int(fields[1]), []).append(int(fields[0]))
        pending = list(children.get(root_pid, ()))
        identities: list[ProcessIdentity] = []
        while pending:
            pid = pending.pop()
            identities.append(ProcessIdentity.capture(pid))
            pending.extend(children.get(pid, ()))
        return tuple(identities)

    def wait(self, owned: OwnedProcess, *, timeout: float) -> int:
        try:
            status = owned.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise
        else:
            self._children.pop(owned.process.pid, None)
            return status

    def terminate_all(self, *, grace_seconds: float = 5.0) -> tuple[int, ...]:
        terminated: list[int] = []
        children = tuple(self._children.values())
        for owned in children:
            try:
                owned.identity.assert_alive()
                os.killpg(owned.identity.pgid, signal.SIGTERM)
                terminated.append(owned.identity.pid)
            except (ProcessLookupError, subprocess.CalledProcessError):
                pass
        deadline = time.monotonic() + max(0.0, grace_seconds)
        for owned in children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                owned.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                # Recheck identity immediately before the irreversible group signal.
                owned.identity.assert_alive()
                os.killpg(owned.identity.pgid, signal.SIGKILL)
                owned.process.wait()
            finally:
                self._children.pop(owned.process.pid, None)
        return tuple(terminated)


def require_darwin_tools(tools: Sequence[Path]) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("native profiler collection is supported only on macOS")
    for tool in tools:
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise FileNotFoundError(f"required profiler tool is unavailable: {tool}")


def wait_for_profiler_release(stream: TextIO = sys.stdin) -> None:
    """Hold a verified checkpoint; EOF is failure rather than implicit continuation."""

    if stream.readline() == "":
        raise EOFError("profiler checkpoint input closed before release")


def profiler_commands(pid: int, output: Path) -> tuple[ProfilerCommand, ...]:
    if pid <= 0:
        raise ValueError("profiler PID must be positive")
    root = output.expanduser().resolve()

    def artifact(suffix: str) -> Path:
        return Path(f"{root}.{suffix}.txt")

    sample_output = artifact("sample")
    return (
        ProfilerCommand(
            ("/usr/bin/sample", str(pid), "10", "1", "-file", str(sample_output)),
            sample_output,
            True,
        ),
        ProfilerCommand(("/usr/bin/heap", str(pid)), artifact("heap")),
        ProfilerCommand(("/usr/bin/vmmap", str(pid)), artifact("vmmap")),
        ProfilerCommand(("/usr/bin/leaks", str(pid)), artifact("leaks")),
        ProfilerCommand(
            ("/usr/bin/malloc_history", str(pid), "-allBySize"),
            artifact("malloc_history"),
        ),
    )


def collect_profiler_artifacts(
    target: ProcessIdentity,
    output: Path,
    *,
    deadline: float,
    include: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Profile one identity-checked audit child within the shared absolute deadline."""

    plans = tuple(
        plan
        for plan in profiler_commands(target.pid, output)
        if include is None or Path(plan.argv[0]).name in include
    )
    require_darwin_tools(tuple(Path(plan.argv[0]) for plan in plans))
    target.assert_alive()
    registry = OwnedProcessRegistry()
    results: list[dict[str, Any]] = []
    try:
        for plan in plans:
            target.assert_alive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("shared audit deadline expired during profiler collection")
            plan.output.parent.mkdir(parents=True, exist_ok=True)
            if plan.output.exists():
                raise FileExistsError(f"profiler evidence already exists: {plan.output}")
            if plan.tool_writes_output:
                owned = registry.spawn(
                    plan.argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                status = registry.wait(owned, timeout=remaining)
            else:
                with plan.output.open("xb") as stream:
                    owned = registry.spawn(
                        plan.argv,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                    )
                    status = registry.wait(owned, timeout=remaining)
            if status != 0:
                raise subprocess.CalledProcessError(status, plan.argv)
            results.append(
                {
                    "tool": Path(plan.argv[0]).name,
                    "path": str(plan.output),
                    "sha256": sha256_file(plan.output),
                    "bytes": plan.output.stat().st_size,
                    "target": {
                        "pid": target.pid,
                        "pgid": target.pgid,
                        "started": target.started,
                        "command": target.command,
                    },
                }
            )
    finally:
        registry.terminate_all()
    return tuple(results)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def publish_json_exclusive(path: Path, value: object) -> None:
    """Fsync and atomically publish JSON without replacing existing evidence."""

    path = path.expanduser().resolve()
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
