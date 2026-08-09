"""Shared frontend policy for Enter and AnyKey message waits."""

from __future__ import annotations

from typing import Any

from .wire import variant


def is_message_wait(wait: dict[int, Any] | None) -> bool:
    """Return whether a wait can be continued without a value."""

    return wait is not None and wait.get(1) in (0, 1)


def is_message_skip_wait(wait: dict[int, Any] | None) -> bool:
    """Return whether a wait may start continuous message skipping."""

    return is_message_wait(wait) and not wait.get(4, False)


def message_wait_intent(wait: dict[int, Any], any_key_value: str = "\n") -> list[Any]:
    """Build the protocol intent matching an Enter or AnyKey wait."""

    if wait.get(1) == 0:
        return variant(0)
    if wait.get(1) == 1:
        return variant(1, any_key_value or "\n")
    raise ValueError("message wait intent requires an Enter or AnyKey wait")
