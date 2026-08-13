from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


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


def _worker_class() -> type[Any]:
    from .worker import RuntimeWorker

    return RuntimeWorker


def _app_class() -> type[Any]:
    from .app import RustyEraTui

    return RustyEraTui


def _shutdown_worker(worker: Any) -> None:
    shutdown = getattr(worker, "shutdown", None)
    if shutdown is not None:
        shutdown()
        return
    if worker.is_alive():
        worker.stop()
    if worker.ident is not None:
        worker.join()


def main() -> None:
    args = build_parser().parse_args()
    resource_directory = args.resource_directory
    project_file = None if resource_directory is not None else args.project_file
    if resource_directory is None and project_file is None:
        resource_directory = Path.cwd()
    # Start project scanning/cache import before importing and constructing Textual. The bounded
    # worker event queue safely retains startup output until the UI mounts.
    worker = _worker_class()(
        args.runtime_library,
        resource_directory,
        initial_project_file=project_file,
    )
    worker.start()
    try:
        _app_class()(
            resource_directory=resource_directory,
            runtime_library=args.runtime_library,
            project_file=project_file,
            worker=worker,
        ).run()
    finally:
        _shutdown_worker(worker)


if __name__ == "__main__":
    main()
