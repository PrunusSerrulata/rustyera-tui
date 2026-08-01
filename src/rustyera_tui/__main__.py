from __future__ import annotations

import argparse
from pathlib import Path

from .app import RustyEraTui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a RustyEra game in a Textual TUI")
    parser.add_argument(
        "resource_directory",
        nargs="?",
        type=Path,
        help="resource directory containing CSV and ERB files (takes priority over --project-file)",
    )
    parser.add_argument(
        "--project-file",
        type=Path,
        help="path to a self-contained .reraproj file",
    )
    parser.add_argument(
        "--runtime-library",
        type=Path,
        help="path to the era-runtime-capi dynamic library",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    resource_directory = args.resource_directory
    project_file = None if resource_directory is not None else args.project_file
    if resource_directory is None and project_file is None:
        resource_directory = Path.cwd()
    RustyEraTui(
        resource_directory=resource_directory,
        runtime_library=args.runtime_library,
        project_file=project_file,
    ).run()


if __name__ == "__main__":
    main()
