# tests/test_enclave_gzip_scope.py
"""Coverage gap that hid the GZipMiddleware-scope regression (whole-branch
review BLOCKER): Starlette's stock GZipMiddleware compresses ANY response
>= minimum_size regardless of content-type or status, which broke the binary
/image route and its 206 Range partials (spec §6). ContentTypeGZipMiddleware
(enclave/routes/gzip.py) restores flask-compress's actual scoping: only
status-200 + allowlisted (text/JSON) Content-Type gets gzipped.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402

FRAME_ID = "ab" * 8
JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 4  # 1027 bytes，> 500B 阈值


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _wired(monkeypatch):
    """Same wiring as tests/test_enclave_routes_frames.py: fake whoami/envelope
    backend calls + fake decrypt returning a JPEG-carrying inner payload."""
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == f"/v1/screen/frames/{FRAME_ID}/envelope"
        return {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                "owner_user_id": "usr_a", "ts": 1.0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    inner = {"image": base64.b64encode(JPEG).decode(), "image_mime": "image/jpeg",
             "ocr_text": "text on screen", "app": "Safari", "w": 100, "h": 200}
    monkeypatch.setattr(envmod, "decrypt_envelope",
                        lambda e, u, s: json.dumps(inner).encode())


# ---- JSON still compresses (no regression) ----

def test_attestation_json_compresses(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 1.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "ab" * 8000,  # 16KB，远超 500B 阈值
        "event_log_json": "[]", "measurements": {}, "compose_hash": "h",
        "app_id": "a", "instance_id": "i",
    })
    r = client.get("/attestation", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert len(r.get_json()["tdx_quote_hex"]) == 16000  # httpx 已自动解压


# ---- binary /image is never compressed ----

def test_image_full_200_not_compressed(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None
    assert r.data == JPEG  # 原样往返，没被 gzip 篡改


def test_image_range_206_not_compressed_and_content_range_correct(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Accept-Encoding": "gzip",
                            "Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers.get("content-encoding") is None
    assert r.headers["content-range"] == f"bytes 0-99/{len(JPEG)}"
    assert r.data == JPEG[:100]


# ---- below the minimum_size threshold: never compressed ----

def test_small_json_below_threshold_not_compressed(client):
    r = client.get("/healthz", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None
    body = r.get_json()
    assert body["ok"] is True and body["ready"] is True
