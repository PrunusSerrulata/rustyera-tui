"""Validation and projection of core-owned project compatibility identities."""

from __future__ import annotations

from typing import Any


def compatibility_identity(value: Any) -> dict[int, Any]:
    """Validate the public identity without interpreting or inventing runtime policy."""

    if not isinstance(value, dict) or set(value) != set(range(9)):
        raise ValueError("Runtime returned an invalid compatibility identity")
    if type(value[0]) is not int or value[0] not in (0, 1):
        raise ValueError("Runtime returned an unsupported compatibility profile")
    for key in (1, 2, 5):
        if type(value[key]) is not int or not 0 < value[key] <= 0xFFFF_FFFF:
            raise ValueError("Runtime returned an invalid compatibility version")
    for key in (3, 4, 6, 7):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError("Runtime returned an invalid compatibility policy")
    services = value[8]
    if not isinstance(services, list):
        raise ValueError("Runtime returned invalid compatibility services")
    for service in services:
        if (
            not isinstance(service, dict)
            or set(service) != {0, 1}
            or not isinstance(service[0], str)
            or not service[0]
            or type(service[1]) is not int
            or not 0 < service[1] <= 0xFFFF_FFFF
        ):
            raise ValueError("Runtime returned an invalid compatibility service")
    return {**value, 8: [dict(service) for service in services]}


def compatibility_profile(value: dict[int, Any]) -> str:
    return ("emuera.em", "emuera.skia.snake")[compatibility_identity(value)[0]]


def configuration_digest(value: Any) -> bytes | None:
    if value is not None and (not isinstance(value, bytes) or len(value) != 32):
        raise ValueError("Runtime returned an invalid configuration digest")
    return value


def compatibility_context(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts = []
    identity = value.get(0)
    if identity is not None:
        try:
            identity = compatibility_identity(identity)
            parts.append(f"profile={compatibility_profile(identity)}@{identity[1]}/{identity[2]}")
        except ValueError:
            parts.append("profile=<invalid>")
    if value.get(1):
        parts.append(f"stage={value[1]}")
    if value.get(2):
        parts.append(f"api={value[2]}")
    required = value.get(3)
    if isinstance(required, dict):
        from .protocol_text import SERVICE_KINDS, enum_text

        version = required.get(2) or {}
        kind = enum_text(required.get(0), SERVICE_KINDS, "ServiceKind")
        parts.append(
            f"requires={kind}.{required.get(1, '?')}@{version.get(0, '?')}.{version.get(1, '?')}"
        )
    return " ".join(parts)
