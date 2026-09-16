"""T620: the worker's failure log/trajectory may carry a REGISTERED exception
code (which validator raised) and never free text — allowlist, not shape."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import generated_image  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402


@pytest.mark.parametrize("message", sorted(generated_image.GENERATED_IMAGE_REJECT_CODES))
def test_registered_reject_codes_pass_through(message):
    assert worker._known_exception_detail(ValueError(message)) == message


@pytest.mark.parametrize("message", [
    "",
    "image generation failed",                        # prose
    "Expecting value: line 1 column 1 (char 0)",       # json decoder prose
    "/Users/someone/private/file.png",                # path
    "file:/users/alice/private.png",                  # codex4 counterexample
    "sk_test_synthetic_secret_123",                   # codex4 counterexample: slug-shaped token
    "private_user_message",                           # codex4 counterexample: slug-shaped content
    "plaintext_envelope_required",                    # real slug, but not registered here
    "generated_image_too_large ",                     # trailing space is stripped → still exact match required
    "GENERATED_IMAGE_TOO_LARGE",
    "{\"user\": \"secret\"}",
])
def test_unregistered_messages_are_dropped(message):
    got = worker._known_exception_detail(ValueError(message))
    assert got in ("", "generated_image_too_large") and (got == "" or message.strip() == got)


def test_every_reject_literal_raised_by_generated_image_is_registered():
    # Derivation guard: the allowlist must not drift from the raise sites.
    source = (Path(__file__).parent.parent / "backend" / "generated_image.py").read_text()
    raised = set(re.findall(r'raise ValueError\("([a-z_]+)"\)', source))
    assert raised, "expected literal ValueError codes in generated_image.py"
    assert raised <= generated_image.GENERATED_IMAGE_REJECT_CODES
    assert "generated_image_invalid" in generated_image.GENERATED_IMAGE_REJECT_CODES


@pytest.mark.parametrize("exc,expected", [
    (ValueError("generated_image_too_large"), "generated_image_too_large"),
    (ValueError("Decompressed Data Too Large"), "generated_image_invalid"),   # Pillow free text
    (ValueError("file:/users/alice/private.png"), "generated_image_invalid"),
    (ValueError(""), "generated_image_invalid"),
])
def test_reject_code_is_a_closed_set(exc, expected):
    assert generated_image.reject_code(exc) == expected


@pytest.mark.parametrize("value,expected", [
    ("image/png", "image/png"), ("IMAGE/JPEG", "image/jpeg"), ("image/webp; charset=x", "image/webp"),
    ("", ""), (None, ""), ("image/svg+xml", "other"), ("text/html; user=secret", "other"),
    ("application/json {\"secret\": 1}", "other"),
])
def test_canonical_declared_mime_is_a_closed_set(value, expected):
    assert generated_image.canonical_declared_mime(value) == expected
