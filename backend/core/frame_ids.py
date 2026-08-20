"""Shared validation for frame and stored-photo identifiers."""
from __future__ import annotations

import re


_SUPPORTED_FRAME_ID_RE = re.compile(r"^[a-f0-9]{16,64}$")


def is_supported_frame_id(value: object) -> bool:
    """Return whether *value* can be addressed by every frame read route."""
    return isinstance(value, str) and _SUPPORTED_FRAME_ID_RE.fullmatch(value) is not None
