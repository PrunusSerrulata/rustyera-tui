"""Textual application shell for the RustyEra runtime frontend."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .app_shell import RustyEraTui as RustyEraTui

CORE_VERSION = "0.6.0 (a75d8e5f)"


def frontend_version() -> str:
    try:
        return version("rustyera-tui")
    except PackageNotFoundError:
        return "0.6.0"


RustyEraTui.__module__ = __name__
