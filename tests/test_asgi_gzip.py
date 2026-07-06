"""ASGI response compression parity (ASGI-migration plan §5 保鲜项).

Flask had ``Compress(app)`` (flask-compress, app.py:411 pre-cutover): CVM egress
is ~30-50 KB/s, and large JSON payloads (decrypt-with-image was 470 KB) rely on
gzip for a 3-5x latency win. The cutover dropped it — the plan left "GZipMiddleware
或下放 ingress" as an open decision and neither landed. These tests pin the
GZipMiddleware behavior:

  * a large response IS gzipped when the client sends ``Accept-Encoding: gzip``,
  * the gzipped body decodes to the identical JSON as the identity response,
  * a small response (< minimum_size, e.g. /healthz) is NOT compressed,
  * no compression when the client does not accept gzip.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    """Fresh, isolated registry/store state per test."""
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    return tmp_path


@pytest.fixture()
def api_key(clean):
    res = make_client().post("/v1/users/register", json={})
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["api_key"]


def _asgi_get(path: str, headers: dict):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get(path, headers=headers)

    return asyncio.run(go())


def _asgi_post(path: str, headers: dict, json_body: dict):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post(path, headers=headers, json=json_body)

    return asyncio.run(go())


def test_large_response_gzipped_when_client_accepts(api_key):
    # /v1/bootstrap is one-shot per user: the FIRST call returns the multi-KB
    # instructions doc (deterministically over any sane minimum_size), later
    # calls a short ack — so the gzip probe must be this user's first call.
    auth = {"Authorization": f"Bearer {api_key}"}
    gz = _asgi_post("/v1/bootstrap", {**auth, "Accept-Encoding": "gzip"}, {})
    assert gz.status_code == 200
    assert gz.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in gz.headers.get("vary", "").lower()
    # httpx transparently decompresses: .content is the decoded payload, while
    # content-length is the on-the-wire (compressed) size.
    assert len(gz.content) > 1000
    assert int(gz.headers["content-length"]) < len(gz.content)
    assert gz.json()  # decoded payload is valid JSON


def test_small_response_not_compressed(api_key):
    resp = _asgi_get("/healthz", {"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.json() == {"ok": True, "mode": "multi_tenant"}


def test_no_compression_when_client_does_not_accept_gzip(api_key):
    # First (large) bootstrap call, but the client only accepts identity.
    auth = {"Authorization": f"Bearer {api_key}"}
    resp = _asgi_post("/v1/bootstrap", {**auth, "Accept-Encoding": "identity"}, {})
    assert resp.status_code == 200
    assert len(resp.content) > 1000
    assert "content-encoding" not in resp.headers
