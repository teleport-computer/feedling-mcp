"""Background screen reads (model_api context / proactive caption) must forward the
Scoped runtime token for Runtime V2 workers (no api_key), not return api_key_unavailable.
Covers screen.frames._enclave_auth_headers + screen.caption._enclave_auth_headers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from core import reqctx  # noqa: E402
from screen import frames as screen_frames  # noqa: E402
from screen import caption as screen_caption  # noqa: E402

_MODS = [screen_frames, screen_caption]


@pytest.mark.parametrize("mod", _MODS)
def test_picks_request_runtime_token_when_no_api_key(mod):
    with reqctx.bind(headers={"X-Feedling-Runtime-Token": "rt_1"}):
        assert mod._enclave_auth_headers(None) == {"X-Feedling-Runtime-Token": "rt_1"}


@pytest.mark.parametrize("mod", _MODS)
def test_explicit_runtime_token_preferred(mod):
    assert mod._enclave_auth_headers("ak", runtime_token="rt_x") == {"X-Feedling-Runtime-Token": "rt_x"}


@pytest.mark.parametrize("mod", _MODS)
def test_api_key_fallback_outside_request(mod):
    assert mod._enclave_auth_headers("ak") == {"X-API-Key": "ak"}


@pytest.mark.parametrize("mod", _MODS)
def test_none_when_no_credential(mod):
    assert mod._enclave_auth_headers(None) is None
