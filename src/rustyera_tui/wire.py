"""Numeric CBOR projections of the public RustyEra wire contracts.

The Rust protocol uses minicbor maps with integer field keys. Keeping this module limited to
wire construction and structural decoding makes schema drift visible and testable without
duplicating Rust's internal runtime types in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cbor2

WIRE_VERSION = (2, 0)
RUNTIME_VERSION = (24, 0)
DEBUG_VERSION = (4, 0)

CHANNEL_RUNTIME = 0
CHANNEL_DEBUG = 1


def version(major: int, minor: int) -> dict[int, int]:
    return {0: major, 1: minor}


def version_range(major: int, minor: int) -> dict[int, dict[int, int]]:
    selected = version(major, minor)
    return {0: selected, 1: selected}


def variant(tag: int, *fields: Any) -> list[Any]:
    """Encode minicbor's default enum representation.

    A non-index-only enum is `[variant-index, variant-fields-array]`. The second array may be
    empty for unit variants. This helper is deliberately small so nested message, command,
    and intent enums all use the exact same representation.
    """

    return [tag, list(fields)]


def unwrap_variant(value: Any) -> tuple[int, list[Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"expected a two-element enum array, got {value!r}")
    tag, fields = value
    if not isinstance(tag, int) or not isinstance(fields, list):
        raise ValueError(f"invalid enum projection: {value!r}")
    return tag, fields


def encode(value: Any) -> bytes:
    # canonical=True selects shortest integer widths and deterministic map ordering. All
    # protocol map keys are small integers, matching Rust's bytewise-key wire profile.
    return cbor2.dumps(value, canonical=True)


def decode(data: bytes) -> Any:
    return cbor2.loads(data)


@dataclass(frozen=True, slots=True)
class DecodedEnvelope:
    channel: int
    channel_version: tuple[int, int]
    session: dict[int, int] | None
    sequence: int
    message_id: int
    correlation_id: int | None
    payload_tag: int
    payload: Any
    epoch: int | None


def encode_envelope(
    *,
    channel: int,
    channel_version: tuple[int, int],
    session: dict[int, int] | None,
    sequence: int,
    message_id: int,
    correlation_id: int | None,
    payload_tag: int,
    payload: Any,
    epoch: int | None,
) -> bytes:
    envelope: dict[int, Any] = {
        0: version(*WIRE_VERSION),
        1: version(*channel_version),
        2: channel,
        4: sequence,
        5: message_id,
        7: payload_tag,
        8: encode(payload),
    }
    # minicbor omits absent Option fields in map-encoded structures.
    if session is not None:
        envelope[3] = session
    if correlation_id is not None:
        envelope[6] = correlation_id
    if epoch is not None:
        envelope[9] = epoch
    return encode(envelope)


def decode_envelope(data: bytes) -> DecodedEnvelope:
    raw = decode(data)
    if not isinstance(raw, dict):
        raise ValueError("envelope is not a CBOR map")
    wire = raw.get(0, {})
    if wire.get(0) != WIRE_VERSION[0]:
        raise ValueError(f"unsupported wire version {wire!r}")
    channel_version = raw.get(1, {})
    payload_bytes = raw.get(8)
    if not isinstance(payload_bytes, bytes):
        raise ValueError("envelope payload is not a CBOR byte string")
    return DecodedEnvelope(
        channel=raw[2],
        channel_version=(channel_version[0], channel_version[1]),
        session=raw.get(3),
        sequence=raw[4],
        message_id=raw[5],
        correlation_id=raw.get(6),
        payload_tag=raw[7],
        payload=decode(payload_bytes),
        epoch=raw.get(9),
    )


def runtime_message(tag: int, value: Any | None = None) -> list[Any]:
    return variant(tag, value) if value is not None else variant(tag)


def debug_message(tag: int, value: Any | None = None) -> list[Any]:
    return variant(tag, value) if value is not None else variant(tag)


def message_value(payload: Any, expected_tag: int | None = None) -> Any:
    tag, fields = unwrap_variant(payload)
    if expected_tag is not None and tag != expected_tag:
        raise ValueError(f"payload tag {tag} does not match envelope tag {expected_tag}")
    return fields[0] if fields else None
