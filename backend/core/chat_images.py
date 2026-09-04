"""Versioned binary storage for one chat message containing multiple images.

The bundle is encrypted as the message's existing main binary envelope, so the
normal single-object R2 offload remains the only heavy-body storage path.  A
single image deliberately never uses this codec: its historical envelope and
metadata shape stay byte-for-byte unchanged.
"""
from __future__ import annotations

import struct


MAX_CHAT_IMAGES_PER_MESSAGE = 9
CHAT_IMAGE_BUNDLE_VERSION = 1
MAX_CHAT_IMAGE_OBSERVATION_CHARS = 12_000
MULTI_IMAGE_STORAGE_FIELDS = (
    "image_bundle_version",
    "image_count",
    "image_mimes",
)
_MAGIC = b"FLIM\x01"
_MIME_TO_CODE = {
    "image/jpeg": 1,
    "image/jpg": 1,
    "image/png": 2,
    "image/webp": 3,
    "image/gif": 4,
}
_CODE_TO_MIME = {
    1: "image/jpeg",
    2: "image/png",
    3: "image/webp",
    4: "image/gif",
}


def encode_image_bundle(images: list[tuple[bytes, str]]) -> bytes:
    """Encode 2..9 validated images without base64-inside-base64 expansion."""
    if not 2 <= len(images) <= MAX_CHAT_IMAGES_PER_MESSAGE:
        raise ValueError("chat image bundle requires 2..9 images")
    out = bytearray(_MAGIC)
    out.append(len(images))
    for body, mime in images:
        code = _MIME_TO_CODE.get(str(mime).lower())
        if code is None or not isinstance(body, bytes) or not body:
            raise ValueError("invalid chat image bundle item")
        out.extend(struct.pack(">BI", code, len(body)))
        out.extend(body)
    return bytes(out)


def decode_image_bundle(body: bytes) -> list[tuple[bytes, str]]:
    """Strictly decode a stored bundle; reject truncation and trailing bytes."""
    if not isinstance(body, bytes) or not body.startswith(_MAGIC):
        raise ValueError("invalid chat image bundle magic")
    offset = len(_MAGIC)
    if offset >= len(body):
        raise ValueError("missing chat image bundle count")
    count = body[offset]
    offset += 1
    if not 2 <= count <= MAX_CHAT_IMAGES_PER_MESSAGE:
        raise ValueError("invalid chat image bundle count")
    images: list[tuple[bytes, str]] = []
    for _index in range(count):
        if offset + 5 > len(body):
            raise ValueError("truncated chat image bundle header")
        code, size = struct.unpack_from(">BI", body, offset)
        offset += 5
        mime = _CODE_TO_MIME.get(code)
        if mime is None or size <= 0 or offset + size > len(body):
            raise ValueError("invalid chat image bundle item")
        images.append((body[offset:offset + size], mime))
        offset += size
    if offset != len(body):
        raise ValueError("trailing chat image bundle bytes")
    return images


def combine_numbered_observations(observations: list[str]) -> str:
    """Combine per-image observations under one message-wide character cap."""
    cleaned = [str(item or "").strip() for item in observations]
    if not cleaned or any(not item for item in cleaned):
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    prefixes = [f"Image {index}:\n" for index in range(1, len(cleaned) + 1)]
    overhead = sum(map(len, prefixes)) + 2 * (len(cleaned) - 1)
    per_image = max(1, (MAX_CHAT_IMAGE_OBSERVATION_CHARS - overhead) // len(cleaned))
    parts = [
        prefix + observation[:per_image]
        for prefix, observation in zip(prefixes, cleaned)
    ]
    return "\n\n".join(parts)
