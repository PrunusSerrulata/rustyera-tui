"""Portable image-header decoding for runtime resource metadata requests."""

from __future__ import annotations

import struct


def decode_image_metadata(data: bytes) -> dict[int, object]:
    """Return the public image metadata projection without using platform graphics APIs."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = _png_size(data)
        return _metadata(width, height, "png", _png_is_animated(data))
    if data[:2] == b"BM":
        width, height = _bmp_size(data)
        return _metadata(width, height, "bmp", False)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = _gif_size(data)
        return _metadata(width, height, "gif", b"NETSCAPE2.0" in data)
    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_size(data)
        return _metadata(width, height, "jpeg", False)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height, animated = _webp_size(data)
        return _metadata(width, height, "webp", animated)
    raise ValueError("unsupported or malformed image resource")


def _metadata(width: int, height: int, format_name: str, animated: bool) -> dict[int, object]:
    if not (0 < width <= 0xFFFF_FFFF and 0 < height <= 0xFFFF_FFFF):
        raise ValueError("image dimensions are out of range")
    return {0: width, 1: height, 2: format_name, 3: animated}


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ValueError("malformed PNG header")
    return struct.unpack_from(">II", data, 16)


def _png_is_animated(data: bytes) -> bool:
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return False
        if kind == b"acTL":
            return True
        if kind == b"IEND":
            return False
        offset = end
    return False


def _bmp_size(data: bytes) -> tuple[int, int]:
    if len(data) < 26:
        raise ValueError("malformed BMP header")
    dib_size = int.from_bytes(data[14:18], "little")
    if dib_size == 12:
        return struct.unpack_from("<HH", data, 18)
    if dib_size < 40 or len(data) < 26:
        raise ValueError("unsupported BMP header")
    width, height = struct.unpack_from("<ii", data, 18)
    return abs(width), abs(height)


def _gif_size(data: bytes) -> tuple[int, int]:
    if len(data) < 10:
        raise ValueError("malformed GIF header")
    return struct.unpack_from("<HH", data, 6)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    offset = 2
    start_of_frame = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
    while offset < len(data):
        while offset < len(data) and data[offset] != 0xFF:
            offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in start_of_frame:
            if length < 7:
                break
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    raise ValueError("malformed JPEG header")


def _webp_size(data: bytes) -> tuple[int, int, bool]:
    offset = 12
    while offset + 8 <= len(data):
        kind = data[offset : offset + 4]
        length = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload = data[offset + 8 : offset + 8 + length]
        if len(payload) != length:
            break
        if kind == b"VP8X" and length >= 10:
            width = 1 + int.from_bytes(payload[4:7], "little")
            height = 1 + int.from_bytes(payload[7:10], "little")
            return width, height, bool(payload[0] & 0x02)
        if kind == b"VP8 " and length >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            return width, height, False
        if kind == b"VP8L" and length >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            return width, height, False
        offset += 8 + length + (length & 1)
    raise ValueError("malformed WebP header")
