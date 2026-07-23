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
        default=Path.cwd(),
        help="resource directory containing CSV, ERB, and the runtime library (default: cwd)",
    )
    parser.add_argument(
        "--runtime-library",
        type=Path,
        help="path to the era-runtime-capi dynamic library",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    RustyEraTui(
        resource_directory=args.resource_directory,
        runtime_library=args.runtime_library,
    ).run()


if __name__ == "__main__":
    main()
