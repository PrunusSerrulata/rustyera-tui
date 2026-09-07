"""Thin CLI, validation, and orchestration surface for deterministic TUI audits.

Real capture and replay use the public C ABI through the existing headless testing lifecycle.
The TUI trace keeps its truthful client capabilities and must not be presented as a directly
replayable Core trace without an explicit semantic adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .performance_process import (
    collect_profiler_artifacts as collect_profiler_artifacts,
    profiler_commands,
    wait_for_profiler_release as wait_for_profiler_release,
)
from .performance_replay import AuditWatchdog as AuditWatchdog
from .performance_replay import ReplayVerifier as ReplayVerifier
from .performance_trace import (
    SNAKE_PROFILE,
    PerformanceTrace,
    PerformanceTraceError,
    capture_template,
    freeze_candidate,
    project_digest,
)

AUDIT_WALL_CLOCK_SECONDS = 60 * 60
MAX_AUDIT_ITERATIONS = 100
SCENARIO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_snake_project_pair(source: Path, project: Path) -> tuple[Path, Path, str]:
    source = source.expanduser().resolve(strict=True)
    project = project.expanduser().resolve(strict=True)
    if source == project or source.is_relative_to(project) or project.is_relative_to(source):
        raise PerformanceTraceError(
            "source and isolated audit project trees must differ and be separate"
        )
    for label, root in (("source", source), ("isolated", project)):
        configuration = root / "reraconfig.toml"
        try:
            profile = tomllib.loads(configuration.read_text(encoding="utf-8"))["compatibility"][
                "profile"
            ]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise PerformanceTraceError(
                f"{label} project lacks a valid explicit compatibility profile"
            ) from error
        if profile != SNAKE_PROFILE:
            raise PerformanceTraceError(f"{label} project is not a snake-profile project")
    source_digest = project_digest(source)
    if project_digest(project) != source_digest:
        raise PerformanceTraceError("isolated audit project differs from its source")
    return source, project, source_digest


class AuditBudget:
    """One wall-clock deadline shared by calibration, warmup, baselines, and profiles."""

    def __init__(self, *, seconds: float = AUDIT_WALL_CLOCK_SECONDS) -> None:
        if seconds <= 0:
            raise ValueError("audit budget must be positive")
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        from .performance_process import OwnedProcessRegistry

        self.processes = OwnedProcessRegistry()
        self.incomplete_phases: list[str] = []

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def require_remaining(self, phase: str) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            self.incomplete_phases.append(phase)
            self.terminate_registered()
            raise TimeoutError(f"shared performance audit deadline expired before {phase}")
        return remaining

    def terminate_registered(self, *, grace_seconds: float = 5.0) -> tuple[int, ...]:
        """Terminate only identity-checked child groups spawned by this audit."""

        return self.processes.terminate_all(grace_seconds=grace_seconds)


@dataclass(frozen=True, slots=True)
class AuditSpec:
    source_project: Path
    project: Path
    profile: str
    scenario: str
    trace: Path
    iterations: int
    output: Path
    pause_at: str | None = None
    pause_iteration: int | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> AuditSpec:
        source, project, isolated_digest = validate_snake_project_pair(
            args.source_project, args.project
        )
        if args.profile != SNAKE_PROFILE:
            raise PerformanceTraceError(f"--profile must be {SNAKE_PROFILE}")
        if SCENARIO_NAME.fullmatch(args.scenario) is None:
            raise PerformanceTraceError("--scenario must be a non-empty bounded ASCII identifier")
        if not 1 <= args.iterations <= MAX_AUDIT_ITERATIONS:
            raise PerformanceTraceError(
                f"--iterations must be between 1 and {MAX_AUDIT_ITERATIONS}"
            )
        if args.pause_iteration is not None:
            if args.pause_at is None:
                raise PerformanceTraceError("--pause-iteration requires --pause-at")
            if not 0 <= args.pause_iteration < args.iterations:
                raise PerformanceTraceError("--pause-iteration is outside the replay range")
        if args.pause_at is not None and args.iterations > 1 and args.pause_iteration is None:
            raise PerformanceTraceError(
                "a repeated replay with --pause-at requires --pause-iteration"
            )
        trace = PerformanceTrace.load(args.trace, scenario=args.scenario)
        if args.pause_at is not None and args.pause_at not in {
            step["checkpoint"] for step in trace.raw["steps"]
        }:
            raise PerformanceTraceError("--pause-at checkpoint is absent from the trace")
        if isolated_digest != trace.raw["projectDigest"]:
            raise PerformanceTraceError("audit project digest does not match the frozen trace")
        output = args.output.expanduser().resolve()
        if output.exists():
            raise FileExistsError(f"performance output already exists: {output}")
        process_manifest = output.with_suffix(output.suffix + ".processes.json")
        evidence = [process_manifest]
        for phase in ("cpu_profile", "allocation_profile"):
            prefix = output.with_name(f"{output.stem}.{phase}")
            evidence.extend(command.output for command in profiler_commands(1, prefix))
        existing = next((path for path in evidence if path.exists()), None)
        if existing is not None:
            raise FileExistsError(f"performance evidence already exists: {existing}")
        if (
            output == source
            or output.is_relative_to(source)
            or output == project
            or output.is_relative_to(project)
        ):
            raise PerformanceTraceError("performance output must be outside both project trees")
        return cls(
            source,
            project,
            args.profile,
            args.scenario,
            trace.path,
            args.iterations,
            output,
            args.pause_at,
            args.pause_iteration,
        )


def freeze_capture(source: Path, destination: Path) -> None:
    """Canonicalize a real capture whose steps already contain complete normalized states."""

    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"frozen trace already exists: {destination}")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PerformanceTraceError("capture candidate must be an object")
    raw = freeze_candidate(raw)
    # Validate before publishing so partial or hand-authored captures never become replay input.
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        PerformanceTrace.load(temporary, scenario=str(raw.get("scenario", "")))
        # Hard-link publication is atomic and fails if another audit won the destination race.
        os.link(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_audit(spec: AuditSpec, *, runtime_library: Path | None = None) -> None:
    """Supervise all real C ABI rounds without constructing a GUI."""

    from .performance_runner import run_supervised

    budget = AuditBudget()
    run_supervised(spec, budget, runtime_library=runtime_library)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rustyera-perf-audit")
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("capture-template")
    template.add_argument("--scenario", required=True)
    template.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--source-project", type=Path, required=True)
    capture.add_argument("--project", type=Path, required=True)
    capture.add_argument("--profile", required=True)
    capture.add_argument("--scenario", required=True)
    capture.add_argument("--seed", type=int, required=True)
    capture.add_argument("--max-steps", type=int, default=10_000)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--runtime-library", type=Path)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--capture", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--scenario", required=True)
    validate.add_argument("--trace", type=Path, required=True)
    plan = commands.add_parser("profiler-plan")
    plan.add_argument("--checkpoint-manifest", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--source-project", type=Path, required=True)
    replay.add_argument("--project", type=Path, required=True)
    replay.add_argument("--profile", required=True)
    replay.add_argument("--scenario", required=True)
    replay.add_argument("--trace", type=Path, required=True)
    replay.add_argument("--iterations", type=int, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--pause-at")
    replay.add_argument("--pause-iteration", type=int)
    replay.add_argument("--runtime-library", type=Path)
    child = commands.add_parser("_round")
    child.add_argument("--project", type=Path, required=True)
    child.add_argument("--trace", type=Path, required=True)
    child.add_argument("--scenario", required=True)
    child.add_argument("--phase", required=True)
    child.add_argument("--iteration", type=int)
    child.add_argument("--output", type=Path, required=True)
    child.add_argument("--checkpoint-manifest", type=Path)
    child.add_argument("--pause-at")
    child.add_argument("--allocation-tracking", action="store_true")
    child.add_argument("--deadline", type=float, required=True)
    child.add_argument("--runtime-library", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "capture-template":
            if SCENARIO_NAME.fullmatch(args.scenario) is None:
                raise PerformanceTraceError("--scenario must be bounded ASCII")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(capture_template(scenario=args.scenario), indent=2) + "\n")
        elif args.command == "capture":
            if args.profile != SNAKE_PROFILE:
                raise PerformanceTraceError(f"--profile must be {SNAKE_PROFILE}")
            if SCENARIO_NAME.fullmatch(args.scenario) is None:
                raise PerformanceTraceError("--scenario must be bounded ASCII")
            if not 0 <= args.seed <= 0xFFFF_FFFF_FFFF_FFFF:
                raise PerformanceTraceError("--seed must be unsigned 64-bit")
            if not 1 <= args.max_steps <= 10_000:
                raise PerformanceTraceError("--max-steps must be between 1 and 10000")
            source, project, _digest = validate_snake_project_pair(
                args.source_project, args.project
            )
            output = args.output.expanduser().resolve()
            if output.exists() or output.is_relative_to(source) or output.is_relative_to(project):
                raise PerformanceTraceError("capture output must be new and outside project trees")
            from .performance_replay import capture_autonomous

            capture_autonomous(
                project=project,
                scenario_name=args.scenario,
                seed=args.seed,
                output=output,
                runtime_library=args.runtime_library,
                deadline=time.monotonic() + AUDIT_WALL_CLOCK_SECONDS,
                maximum_steps=args.max_steps,
                requests=sys.stdin,
                observations=sys.stdout,
            )
        elif args.command == "freeze":
            freeze_capture(args.capture, args.output)
        elif args.command == "validate":
            PerformanceTrace.load(args.trace, scenario=args.scenario)
        elif args.command == "profiler-plan":
            from .performance_process import ProcessIdentity

            checkpoint = json.loads(
                args.checkpoint_manifest.expanduser().resolve(strict=True).read_text(
                    encoding="utf-8"
                )
            )
            required = {
                "schemaVersion",
                "checkpoint",
                "phase",
                "iteration",
                "pid",
                "pgid",
                "started",
                "command",
                "session",
            }
            if not isinstance(checkpoint, dict) or set(checkpoint) != required:
                raise PerformanceTraceError("profiler plan requires an exact audit checkpoint manifest")
            if (
                checkpoint.get("schemaVersion") != 1
                or type(checkpoint.get("pid")) is not int
                or checkpoint["pid"] <= 0
                or type(checkpoint.get("pgid")) is not int
                or checkpoint["pgid"] <= 0
                or not isinstance(checkpoint.get("started"), str)
                or not checkpoint["started"]
                or not isinstance(checkpoint.get("command"), str)
                or not checkpoint["command"]
                or not isinstance(checkpoint.get("checkpoint"), str)
                or not isinstance(checkpoint.get("phase"), str)
                or not isinstance(checkpoint.get("session"), dict)
            ):
                raise PerformanceTraceError("profiler checkpoint manifest identity is invalid")
            target = ProcessIdentity(
                checkpoint["pid"],
                checkpoint["pgid"],
                checkpoint["started"],
                checkpoint["command"],
            )
            target.assert_alive()
            for command in profiler_commands(target.pid, args.output):
                print(
                    json.dumps(
                        {
                            "command": command.argv,
                            "output": str(command.output),
                            "target": {
                                "pid": target.pid,
                                "pgid": target.pgid,
                                "started": target.started,
                                "command": target.command,
                            },
                        },
                        separators=(",", ":"),
                    )
                )
        elif args.command == "replay":
            spec = AuditSpec.from_args(args)
            run_audit(spec, runtime_library=args.runtime_library)
        else:
            from .performance_runner import run_child_round

            run_child_round(
                project=args.project.expanduser().resolve(strict=True),
                trace_path=args.trace.expanduser().resolve(strict=True),
                scenario=args.scenario,
                runtime_library=args.runtime_library,
                phase=args.phase,
                iteration=args.iteration,
                output=args.output,
                checkpoint_manifest=args.checkpoint_manifest,
                pause_at=args.pause_at,
                allocation_tracking=args.allocation_tracking,
                deadline=args.deadline,
            )
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
