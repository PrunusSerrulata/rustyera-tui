"""Argument schema for the deterministic TUI test command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Build the shared parser for fixed-run and agent-driven test sessions."""

    parser = argparse.ArgumentParser(prog="rustyera-test")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "serve"):
        command = subparsers.add_parser(name)
        command.add_argument("--scenario", type=Path, required=True)
        command.add_argument("--project", type=Path)
        command.add_argument("--runtime-library", type=Path)
        command.add_argument("--trace", type=Path)
        command.add_argument("--reference-command")
        command.add_argument("--reference-path-command")
        command.add_argument("--metrics-threshold-ms", type=float)
    return parser
