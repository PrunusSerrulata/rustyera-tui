"""Authoritative scene replay retained by the non-pixel terminal frontend."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from .wire import unwrap_variant


def empty_scene() -> dict[int, Any]:
    return {0: 0, 1: []}


def normalize_scene(scene: Mapping[int, Any]) -> dict[int, Any]:
    """Validate and deterministically order a scene snapshot without rendering it."""

    if not isinstance(scene, Mapping):
        raise TypeError("scene snapshot must be a map")
    revision = _u64(scene.get(0), "scene revision")
    raw_layers = scene.get(1)
    if not isinstance(raw_layers, list):
        raise TypeError("scene layers must be a list")
    layers = [_normalize_layer(layer, revision) for layer in raw_layers]
    layer_ids = [layer[0] for layer in layers]
    sequences = [layer[1] for layer in layers]
    if len(layer_ids) != len(set(layer_ids)):
        raise ValueError("scene contains duplicate layer IDs")
    if len(sequences) != len(set(sequences)):
        raise ValueError("scene contains duplicate insertion sequences")
    layers.sort(key=lambda layer: (-layer[3], layer[1]))
    normalized = copy.deepcopy(dict(scene))
    normalized[0] = revision
    normalized[1] = layers
    return normalized


def apply_scene_delta(
    scene: Mapping[int, Any], delta: Mapping[int, Any]
) -> dict[int, Any]:
    """Apply one revision-bound delta atomically and return the new scene."""

    current = normalize_scene(scene)
    if not isinstance(delta, Mapping):
        raise TypeError("scene delta must be a map")
    base_revision = _unsigned(delta.get(0), "scene delta base revision")
    new_revision = _unsigned(delta.get(1), "scene delta new revision")
    operations = delta.get(2)
    if base_revision != current[0]:
        raise ValueError(
            f"scene delta starts at {base_revision}, but local revision is {current[0]}"
        )
    if new_revision <= base_revision:
        raise ValueError("scene delta revision must increase")
    if not isinstance(operations, list):
        raise TypeError("scene delta operations must be a list")

    candidate = copy.deepcopy(current)
    for operation in operations:
        tag, fields = unwrap_variant(operation)
        if tag == 0:
            if len(fields) != 1:
                raise ValueError("scene upsert must contain one layer")
            layer = _normalize_layer(fields[0], new_revision)
            existing_index = next(
                (
                    index
                    for index, existing in enumerate(candidate[1])
                    if existing[0] == layer[0]
                ),
                None,
            )
            if existing_index is None:
                if any(existing[1] > layer[1] for existing in candidate[1]):
                    raise ValueError("new scene layer sequence is not monotonic")
                candidate[1].append(layer)
            else:
                existing = candidate[1][existing_index]
                if existing[1] != layer[1]:
                    raise ValueError("scene layer update changed its insertion sequence")
                if layer[11] < existing[11]:
                    raise ValueError("scene layer revision moved backwards")
                candidate[1][existing_index] = layer
        elif tag == 1:
            layer_id = _single_unsigned(fields, "scene layer ID")
            candidate[1] = [layer for layer in candidate[1] if layer[0] != layer_id]
        elif tag == 2:
            depth = _single_integer(fields, "scene depth")
            candidate[1] = [layer for layer in candidate[1] if layer[3] != depth]
        elif tag == 3:
            line_id = _single_unsigned(fields, "scene line ID")
            candidate[1] = [
                layer for layer in candidate[1] if _anchored_line_id(layer[4]) != line_id
            ]
        elif tag == 4:
            if len(fields) != 1:
                raise ValueError("scene replacement must contain one snapshot")
            replacement = normalize_scene(fields[0])
            if replacement[0] != new_revision:
                raise ValueError("replacement scene revision does not match its delta")
            candidate = replacement
        else:
            raise ValueError(f"unsupported scene operation {tag}")
    candidate[0] = new_revision
    return normalize_scene(candidate)


def _normalize_layer(layer: Any, scene_revision: int) -> dict[int, Any]:
    if not isinstance(layer, Mapping):
        raise TypeError("scene layer must be a map")
    normalized = copy.deepcopy(dict(layer))
    normalized[0] = _u64(layer.get(0), "scene layer ID")
    normalized[1] = _u64(layer.get(1), "scene layer sequence")
    normalized[3] = _i64(layer.get(3), "scene layer depth")
    _validate_source(layer.get(2))
    _validate_anchor(layer.get(4))
    _validate_pair(layer.get(5), "scene offset")
    _validate_pair(layer.get(6), "scene size")
    opacity = _unsigned(layer.get(7), "scene opacity", bits=8)
    if opacity > 255:
        raise ValueError("scene opacity must fit u8")
    matrix = layer.get(8)
    if matrix is not None and (
        not isinstance(matrix, list)
        or len(matrix) != 25
        or any(not _fits_signed(value, 64) for value in matrix)
    ):
        raise ValueError("scene color matrix must contain 25 integers")
    scroll_policy = layer.get(9)
    if not _is_integer(scroll_policy) or scroll_policy not in (0, 1):
        raise ValueError("scene scroll policy is invalid")
    interaction = layer.get(10)
    if interaction is not None:
        _validate_interaction(interaction)
    normalized[11] = _u64(layer.get(11), "scene layer revision")
    if normalized[11] > scene_revision:
        raise ValueError("scene layer revision exceeds the scene revision")
    normalized[12] = _i64(layer.get(12), "scene document origin")
    return normalized


def _validate_source(source: Any) -> None:
    tag, fields = unwrap_variant(source)
    if tag in (0, 1):
        if len(fields) != 2 or not isinstance(fields[0], str):
            raise ValueError("scene resource source is invalid")
    elif tag == 2:
        if len(fields) != 2 or not _fits_signed(fields[0], 64):
            raise ValueError("scene canvas source is invalid")
    else:
        raise ValueError(f"unsupported scene source {tag}")
    _u64(fields[1], "scene resource revision")


def _validate_anchor(anchor: Any) -> None:
    tag, fields = unwrap_variant(anchor)
    if tag == 0 and not fields:
        return
    if tag == 1 and len(fields) == 1:
        _u64(fields[0], "scene anchor line ID")
        return
    raise ValueError("scene anchor is invalid")


def _validate_pair(value: Any, name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a map")
    _i64(value.get(0), f"{name} x/width")
    _i64(value.get(1), f"{name} y/height")


def _validate_interaction(interaction: Any) -> None:
    if not isinstance(interaction, Mapping):
        raise TypeError("scene interaction must be a map")
    token = interaction.get(0)
    if not isinstance(token, Mapping):
        raise TypeError("scene interaction token must be a map")
    _u64(token.get(0), "scene interaction token epoch")
    _u64(token.get(1), "scene interaction token ID")
    _validate_protocol_value(interaction.get(1))
    if not isinstance(interaction.get(2), bool):
        raise TypeError("scene interaction enabled must be a boolean")
    for key, name in ((3, "hover source"), (4, "hit-map source")):
        source = interaction.get(key)
        if source is not None:
            try:
                _validate_source(source)
            except (TypeError, ValueError) as error:
                raise ValueError(f"scene interaction {name} is invalid") from error
    title = interaction.get(5)
    if title is not None and not isinstance(title, str):
        raise TypeError("scene interaction title must be a string")


def _validate_protocol_value(value: Any) -> None:
    tag, fields = unwrap_variant(value)
    if len(fields) != 1:
        raise ValueError("scene interaction value must contain one scalar")
    scalar = fields[0]
    if tag == 0:
        _i64(scalar, "scene interaction integer value")
    elif tag == 1:
        if not isinstance(scalar, str):
            raise TypeError("scene interaction string value must be a string")
    elif tag == 2:
        if not isinstance(scalar, bool):
            raise TypeError("scene interaction boolean value must be a boolean")
    elif tag == 3:
        if not isinstance(scalar, bytes):
            raise TypeError("scene interaction byte value must be a byte string")
    else:
        raise ValueError(f"unsupported scene interaction value {tag}")


def _anchored_line_id(anchor: Any) -> int | None:
    tag, fields = unwrap_variant(anchor)
    return int(fields[0]) if tag == 1 and len(fields) == 1 else None


def _single_unsigned(fields: list[Any], name: str) -> int:
    if len(fields) != 1:
        raise ValueError(f"{name} operation must contain one value")
    return _u64(fields[0], name)


def _single_integer(fields: list[Any], name: str) -> int:
    if len(fields) != 1:
        raise ValueError(f"{name} operation must contain one value")
    return _i64(fields[0], name)


def _unsigned_with_bits(value: Any, name: str, *, bits: int) -> int:
    result = _integer(value, name)
    if not 0 <= result <= (1 << bits) - 1:
        raise ValueError(f"{name} must fit u{bits}")
    return result


def _unsigned(value: Any, name: str, *, bits: int = 64) -> int:
    return _unsigned_with_bits(value, name, bits=bits)


def _u64(value: Any, name: str) -> int:
    return _unsigned(value, name, bits=64)


def _i64(value: Any, name: str) -> int:
    result = _integer(value, name)
    if not -(1 << 63) <= result <= (1 << 63) - 1:
        raise ValueError(f"{name} must fit i64")
    return result


def _integer(value: Any, name: str) -> int:
    if not _is_integer(value):
        raise TypeError(f"{name} must be an integer")
    return int(value)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fits_signed(value: Any, bits: int) -> bool:
    return _is_integer(value) and -(1 << (bits - 1)) <= value <= (1 << (bits - 1)) - 1
