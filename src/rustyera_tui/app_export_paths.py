"""Pure default-path construction for app export actions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .diagnosis import diagnosis_project_name


def snapshot_default_path(project: Path | None, now: datetime | None = None) -> Path:
    return _timestamped_path(project, "runtime", "snapshot", now)


def input_replay_default_path(project: Path | None, now: datetime | None = None) -> Path:
    return _timestamped_path(project, "input-replay", "jsonl", now)


def _timestamped_path(
    project: Path | None, prefix: str, extension: str, now: datetime | None
) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return (project or Path.cwd()) / f"{prefix}_{timestamp}.{extension}"


def project_file_default_path(project: Path | None, title: str) -> Path:
    root = project or Path.cwd()
    safe_title = diagnosis_project_name(title.strip() or root.name or "RustyEra项目")
    return root / f"{safe_title}.reraproj"


def log_default_path(project: Path | None, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return (project or Path.cwd()) / f"log_{timestamp}.log"
