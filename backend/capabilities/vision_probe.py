"""Shared visual control images for Model API and resident capability probes."""

from __future__ import annotations

import base64
import random
import struct
import zlib


_COLORS = {
    "red": (235, 35, 35),
    "green": (20, 180, 65),
    "blue": (25, 90, 235),
    "yellow": (250, 210, 10),
}


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def generate_image() -> tuple[str, str]:
    """Return one random four-stripe PNG and its unpredictable answer."""
    names = list(_COLORS)
    random.SystemRandom().shuffle(names)
    # Keep the control large enough to survive provider-side image resizing.
    # The old 96x24 strip was occasionally read as reordered or "tan" by
    # otherwise vision-capable Claude routes.
    width, height = 512, 256
    stripe_width = width // len(names)
    scanlines = bytearray()
    for _ in range(height):
        scanlines.append(0)
        for x in range(width):
            scanlines.extend(_COLORS[names[min(x // stripe_width, 3)]])
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
    )
    png += _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), 9))
    png += _png_chunk(b"IEND", b"")
    return base64.b64encode(png).decode("ascii"), ",".join(names)


def generate_images() -> tuple[list[dict[str, str]], list[str]]:
    """Two independent controls make a blind pass only 1 in 576."""
    generated = [generate_image(), generate_image()]
    return (
        [
            {
                "data_url": f"data:image/png;base64,{encoded}",
                "mime_type": "image/png",
            }
            for encoded, _expected in generated
        ],
        [expected for _encoded, expected in generated],
    )
