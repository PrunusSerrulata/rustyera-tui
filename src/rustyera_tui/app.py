"""Textual application shell for the RustyEra runtime frontend."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .app_shell import RustyEraTui as RustyEraTui
from .version import CORE_REVISION as CORE_REVISION
from .version import CORE_VERSION as CORE_VERSION


def frontend_version() -> str:
    try:
        return version("rustyera-tui")
    except PackageNotFoundError:
        return "0.8.0"


RustyEraTui.__module__ = __name__
