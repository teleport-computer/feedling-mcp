"""Bounded Canvas labels stored beside the encrypted attachment for clients."""

from __future__ import annotations


TITLE_MAX_CHARS = 120
SUBTITLE_MAX_CHARS = 160
_BIDI_CONTROLS = frozenset(
    chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)


def is_canvas_filename(filename: str) -> bool:
    name = str(filename or "").strip().casefold()
    return name.endswith(".io.html") or name.endswith(".html")


def normalize_text(value, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    normalized = " ".join(value.split()).strip()
    if (
        not normalized
        or len(normalized) > max_chars
        or any(not char.isprintable() for char in normalized)
        or any(char in _BIDI_CONTROLS for char in normalized)
    ):
        raise ValueError(f"invalid {field}")
    return normalized


def metadata_from_payload(
    payload: dict,
    *,
    filename: str,
    require_canvas_pair: bool = False,
) -> dict[str, str]:
    """Validate optional Canvas title/subtitle, requiring both for AI delivery."""
    raw_title = payload.get("file_display_title")
    raw_subtitle = payload.get("file_display_subtitle")
    has_title = raw_title is not None
    has_subtitle = raw_subtitle is not None
    is_canvas = is_canvas_filename(filename)

    if (has_title or has_subtitle) and not is_canvas:
        raise ValueError("file display metadata requires a Canvas filename")
    if require_canvas_pair and filename.casefold().endswith(".io.html"):
        if not has_title or not has_subtitle:
            raise ValueError("Canvas delivery requires title and subtitle")
    if not has_title and not has_subtitle:
        return {}
    result: dict[str, str] = {}
    if has_title:
        result["file_display_title"] = normalize_text(
            raw_title,
            field="file_display_title",
            max_chars=TITLE_MAX_CHARS,
        )
    if has_subtitle:
        result["file_display_subtitle"] = normalize_text(
            raw_subtitle,
            field="file_display_subtitle",
            max_chars=SUBTITLE_MAX_CHARS,
        )
    return result
