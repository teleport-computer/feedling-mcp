"""Unit tests for the screen enclave auth-forwarding logic.

Runtime V2 workers authenticate with a scoped runtime token and have
no api_key; the enclave-proxy screen reads must forward that token (not an empty
header) or every hosted-agent screen read fails. The Flask ``_enclave_forward_auth``
wrapper was deleted in the ASGI cutover; this tests the framework-neutral core it
(and its ASGI counterpart) delegate to — ``screen_read_core.enclave_forward_headers``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from screen import screen_read_core  # noqa: E402


def test_enclave_forward_auth_prefers_runtime_token():
    assert screen_read_core.enclave_forward_headers(
        api_key="ak_1", runtime_token="rt_1"
    ) == {"X-Feedling-Runtime-Token": "rt_1"}


def test_enclave_forward_auth_falls_back_to_api_key():
    assert screen_read_core.enclave_forward_headers(
        api_key="ak_1", runtime_token=None
    ) == {"X-API-Key": "ak_1"}


def test_enclave_forward_auth_empty_when_no_credential():
    assert screen_read_core.enclave_forward_headers(
        api_key=None, runtime_token=None
    ) == {}
