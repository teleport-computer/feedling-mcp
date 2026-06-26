"""Business logic for server-managed UI copy.

Thin layer over copytext.store: builds the client bundle and validates/applies
admin edits. (Seeding from the iOS Localizable.xcstrings lives in
tools/seed_copytext.py, which talks to this over the admin HTTP endpoint.)
"""
from __future__ import annotations

from . import store

LANGS = store.LANGS  # ("en", "zh-Hans")


class CopytextValidationError(ValueError):
    """Raised on a malformed edit payload (surfaced as 400 by routes)."""


def build_bundle() -> dict:
    """The payload served at GET /v1/copytext.

    {"revision": <int>, "strings": {key: {lang: value}}}
    """
    return {"revision": store.get_revision(), "strings": store.get_all()}


def _validate_edits(strings: dict, delete: list) -> None:
    if not isinstance(strings, dict):
        raise CopytextValidationError("'strings' must be an object")
    if not isinstance(delete, list) or any(not isinstance(k, str) for k in delete):
        raise CopytextValidationError("'delete' must be a list of key strings")
    for key, by_lang in strings.items():
        if not isinstance(key, str) or not key:
            raise CopytextValidationError("each key must be a non-empty string")
        if not isinstance(by_lang, dict) or not by_lang:
            raise CopytextValidationError(f"'{key}' must map langs to values")
        for lang, value in by_lang.items():
            if lang not in LANGS:
                raise CopytextValidationError(
                    f"'{key}': unsupported lang '{lang}' (allowed: {', '.join(LANGS)})"
                )
            if not isinstance(value, str):
                raise CopytextValidationError(f"'{key}'.{lang} value must be a string")


def apply_edits(payload: dict) -> dict:
    """Validate then persist an admin edit. Returns the write summary.

    payload: {"strings": {key: {lang: value}}, "delete": [key, ...]}
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise CopytextValidationError("request body must be a JSON object")
    strings = payload.get("strings") or {}
    delete = payload.get("delete") or []
    _validate_edits(strings, delete)
    if not strings and not delete:
        raise CopytextValidationError("nothing to do: provide 'strings' and/or 'delete'")
    revision, upserted, deleted = store.apply_edits(strings, delete)
    return {"revision": revision, "upserted": upserted, "deleted": deleted}
