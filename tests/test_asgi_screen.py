"""Native /v1/screen/* + /v1/sources parity vs the Flask oracle.

Asserts the FastAPI routes (``screen.routes_asgi``) return the same
status/body/bytes/key-headers as the Flask blueprint (``screen.routes``) for
every one of the 11 screen reads. Both sides call the same framework-neutral
``screen.screen_read_core``, so the enclave HTTP client is stubbed once on the
shared ``screen_read_core.httpx`` module object and covers both paths — keeping
the test fully offline and the E2E envelope handling identical across frameworks.

E2E focus (decrypt / image / envelope):
  - ``/envelope`` + ``/<filename>`` return the opaque v1 envelope (``body_ct``
    ciphertext) verbatim — the test asserts no plaintext appears server-side.
  - ``/decrypt`` + ``/image`` are pure enclave proxies: the stub captures the
    forwarded credential to prove the caller's runtime token (or api key) is
    relayed to the enclave and that this process only returns the enclave's
    opaque bytes — it never decrypts.

The screen routes gate on ``auth.require_user()`` only (no
``authorize_scope``), so there is no scope-failure (403) case; auth failure is
the 401 path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import runtime_token as rt_mod  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from screen import frames as screen_frames  # noqa: E402
from screen import routes_asgi as screen_asgi  # noqa: E402
from screen import screen_read_core  # noqa: E402

_RT_SECRET = os.environ["FEEDLING_RUNTIME_TOKEN_SECRET"].encode("utf-8")

PUBLIC_BASE = "https://api.test"


def _build_asgi_app() -> FastAPI:
    # Standalone app: the screen router + fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    screen_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    # Deterministic frame URLs on BOTH backends: with FEEDLING_PUBLIC_BASE_URL set,
    # frames._frame_url never falls back to flask.request.host_url (which has no
    # equivalent in the ASGI core), so the two surfaces produce identical URLs.
    monkeypatch.setenv("FEEDLING_PUBLIC_BASE_URL", PUBLIC_BASE)
    # R2 OFF → frame envelopes persist inline in Postgres (no network / boto3).
    for var in ("R2_ENDPOINT", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_FRAMES_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("FEEDLING_ENCLAVE_URL", raising=False)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


_pk = 0


def _register() -> tuple[str, str]:
    global _pk
    _pk += 1
    raw = _pk.to_bytes(32, "big")
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode("ascii"), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _envelope(uid: str, fid: str) -> dict:
    return {
        "v": 1,
        "id": fid,
        "body_ct": base64.b64encode(b"\x00\xffCIPHERTEXT\x80").decode(),
        "nonce": base64.b64encode(b"123456789012").decode(),
        "K_user": base64.b64encode(b"user-key").decode(),
        "K_enclave": base64.b64encode(b"enc-key").decode(),
        "visibility": "shared",
        "owner_user_id": uid,
    }


def _seed_frame(uid: str, *, app_name=None, ocr="") -> tuple[str, float, dict]:
    """Persist one v1 frame envelope the canonical way (``_save_frame_envelope``:
    DB upsert + index append + index-blob persist), so BOTH backends read one
    identical frame and the store never rebuilds/doubles the index."""
    fid = uuid.uuid4().hex  # 32 lowercase hex chars → matches ^[a-f0-9]{16,64}$
    ts = time.time()
    env = _envelope(uid, fid)
    store = core_store.get_store(uid)
    screen_frames._save_frame_envelope(store, {"ts": ts, "w": 100, "h": 200}, env)
    return fid, ts, env


def _mint_rt(uid: str, scope=("screen",)) -> str:
    return rt_mod.mint(
        _RT_SECRET, user_id=uid, runtime_instance_id="ri_test", scope=list(scope), ttl=900.0)


# --------------------------------------------------------------------------- #
# request helpers → parity tuples
# --------------------------------------------------------------------------- #

def _key(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _flask_get(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _flask_get_raw(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    # res.headers is a case-insensitive Werkzeug Headers object.
    return res.status_code, res.data, res.headers.get("Content-Type"), res.headers


def _asgi(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body
    return asyncio.run(go())


def _asgi_raw(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, headers=headers or {}, **kw)
            # resp.headers is a case-insensitive httpx.Headers object.
            return resp.status_code, resp.content, resp.headers.get("content-type"), resp.headers
    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# enclave stub (covers Flask + ASGI via the shared core module)
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, content: bytes, status_code: int, headers: dict):
        self.content = content
        self.status_code = status_code
        self.headers = headers
        self.text = content.decode("utf-8", "replace")


def _install_enclave(monkeypatch, *, content=b"", status=200, headers=None, raise_http=False):
    """Stub screen_read_core.httpx.Client. Returns a list capturing each enclave
    request (url/headers/params) so credential forwarding can be asserted."""
    calls: list[dict] = []
    hdrs = headers or {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
            if raise_http:
                raise httpx.ConnectError("boom")
            return _FakeResp(content, status, hdrs)

    monkeypatch.setattr(screen_read_core.httpx, "Client", _FakeClient)
    return calls


# =========================================================================== #
# auth (401) parity — every route is gated on require_user
# =========================================================================== #

@pytest.mark.parametrize("path", [
    "/v1/screen/ios",
    "/v1/screen/mac",
    "/v1/screen/summary",
    "/v1/sources",
    "/v1/screen/frames",
    "/v1/screen/frames/latest",
    "/v1/screen/frames/abc.env.json",
    "/v1/screen/frames/deadbeefdeadbeef/envelope",
    "/v1/screen/frames/deadbeefdeadbeef/decrypt",
    "/v1/screen/frames/deadbeefdeadbeef/image",
    "/v1/screen/analyze",
])
def test_no_auth_is_401_parity(env, path):
    f = _flask_get(path)
    a = _asgi("GET", path)
    assert f == a == (401, {"error": "unauthorized"})


# =========================================================================== #
# aggregated screen-time reads (JSON)
# =========================================================================== #

def test_ios_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/ios", _key(api_key))
    a = _asgi("GET", "/v1/screen/ios", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["data_source"] == "mock_fallback"  # no frames → fallback


def test_ios_invalid_window_400_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/ios?window_sec=notanumber", _key(api_key))
    a = _asgi("GET", "/v1/screen/ios?window_sec=notanumber", headers=_key(api_key))
    assert f == a == (400, {"error": "invalid window_sec"})


def test_mac_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/mac", _key(api_key))
    a = _asgi("GET", "/v1/screen/mac", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["total_active_minutes"] == 395


def test_summary_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/summary", _key(api_key))
    a = _asgi("GET", "/v1/screen/summary", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert set(f[1]) == {"date", "ios", "mac", "combined"}


def test_sources_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/sources", _key(api_key))
    a = _asgi("GET", "/v1/sources", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert [s["id"] for s in f[1]["sources"]] == ["ios_pip", "mac_monitor"]


# =========================================================================== #
# frame index reads (JSON)
# =========================================================================== #

def test_frames_list_parity(env):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid, app_name="com.apple.MobileSafari")
    f = _flask_get("/v1/screen/frames", _key(api_key))
    a = _asgi("GET", "/v1/screen/frames", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["total"] == 1
    # URL uses the explicit public base (deterministic on both backends).
    assert f[1]["frames"][0]["url"] == f"{PUBLIC_BASE}/v1/screen/frames/{fid}.env.json?user={uid}"


def test_frames_latest_parity(env):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    f = _flask_get("/v1/screen/frames/latest", _key(api_key))
    a = _asgi("GET", "/v1/screen/frames/latest", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["id"] == fid
    assert "image_base64" not in f[1]


def test_frames_latest_empty_404_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/frames/latest", _key(api_key))
    a = _asgi("GET", "/v1/screen/frames/latest", headers=_key(api_key))
    assert f == a == (404, {"error": "no frames yet"})


# =========================================================================== #
# envelope reads — opaque ciphertext, NEVER decrypted server-side
# =========================================================================== #

def test_serve_frame_parity(env):
    uid, api_key = _register()
    fid, _ts, envelope = _seed_frame(uid)
    path = f"/v1/screen/frames/{fid}.env.json"
    f = _flask_get_raw(path, _key(api_key))
    a = _asgi_raw("GET", path, headers=_key(api_key))
    assert f[0] == a[0] == 200
    assert f[1] == a[1]  # byte-identical raw JSON envelope
    assert f[2] == a[2]  # same Content-Type
    assert "application/json" in (f[2] or "")
    # E2E: the served bytes are the opaque envelope (ciphertext), no plaintext.
    served = json.loads(f[1])
    assert served == envelope
    assert served["body_ct"]  # ciphertext present; no "image_b64"/"ocr_text" plaintext
    assert "image_b64" not in served and "ocr_text" not in served


def test_serve_frame_bad_filename_400_parity(env):
    _uid, api_key = _register()
    # A single path segment that literally contains ".." → rejected by the core's
    # traversal guard on both backends (avoids %2F routing-decode differences).
    f = _flask_get("/v1/screen/frames/foo..bar", _key(api_key))
    a = _asgi("GET", "/v1/screen/frames/foo..bar", headers=_key(api_key))
    assert f == a == (400, {"error": "bad filename"})


def test_serve_frame_not_found_404_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/frames/deadbeefdeadbeef.env.json", _key(api_key))
    a = _asgi("GET", "/v1/screen/frames/deadbeefdeadbeef.env.json", headers=_key(api_key))
    assert f == a == (404, {"error": "not found"})


def test_frame_envelope_parity(env):
    uid, api_key = _register()
    fid, _ts, envelope = _seed_frame(uid)
    path = f"/v1/screen/frames/{fid}/envelope"
    f = _flask_get(path, _key(api_key))
    a = _asgi("GET", path, headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1] == envelope  # opaque envelope; server never decrypted it
    assert "image_b64" not in f[1] and "ocr_text" not in f[1]


def test_frame_envelope_not_found_404_parity(env):
    _uid, api_key = _register()
    path = "/v1/screen/frames/deadbeefdeadbeef/envelope"
    f = _flask_get(path, _key(api_key))
    a = _asgi("GET", path, headers=_key(api_key))
    assert f == a == (404, {"error": "not found"})


# =========================================================================== #
# enclave decrypt proxy — decryption happens INSIDE the enclave
# =========================================================================== #

def test_decrypt_proxy_parity_and_credential_forwarding(env, monkeypatch):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test/")
    plaintext = b'{"app":"Safari","ocr_text":"hi","image_b64":"AAAA"}'
    calls = _install_enclave(
        monkeypatch, content=plaintext, status=200,
        headers={"Content-Type": "application/json"})

    path = f"/v1/screen/frames/{fid}/decrypt?include_image=true"
    f = _flask_get_raw(path, _key(api_key))
    a = _asgi_raw("GET", path, headers=_key(api_key))
    assert f[0] == a[0] == 200
    assert f[1] == a[1] == plaintext  # enclave's bytes relayed verbatim
    assert f[2] == a[2]  # same Content-Type
    # Both backends forwarded the api key to the enclave decrypt endpoint, with
    # include_image passed through. The enclave (not this process) did the decrypt.
    assert len(calls) == 2  # one Flask call + one ASGI call
    for c in calls:
        assert c["url"] == f"https://enclave.test/v1/screen/frames/{fid}/decrypt"
        assert c["headers"] == {"X-API-Key": api_key}
        assert c["params"] == {"include_image": "true"}


def test_decrypt_forwards_runtime_token_for_hostall_agent(env, monkeypatch):
    """host-all / zero-roster agent: authenticates with a VALID Stage-D runtime
    token and has NO api_key. The enclave proxy must forward that token (not an
    empty header) — the exact failure the memory readside fix addressed."""
    uid, _api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    calls = _install_enclave(monkeypatch, content=b"{}", status=200,
                             headers={"Content-Type": "application/json"})
    tok = _mint_rt(uid)
    headers = {"X-Feedling-Runtime-Token": tok}
    path = f"/v1/screen/frames/{fid}/decrypt"
    f = _flask_get_raw(path, headers)
    a = _asgi_raw("GET", path, headers)
    assert f[0] == a[0] == 200
    assert len(calls) == 2
    assert all(c["headers"] == {"X-Feedling-Runtime-Token": tok} for c in calls)
    # include_image defaults to "true" when the query param is absent.
    assert all(c["params"] == {"include_image": "true"} for c in calls)


def test_decrypt_frame_not_found_404_parity(env, monkeypatch):
    _uid, api_key = _register()
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    _install_enclave(monkeypatch)  # should never be called
    path = "/v1/screen/frames/deadbeefdeadbeef/decrypt"
    f = _flask_get(path, _key(api_key))
    a = _asgi("GET", path, headers=_key(api_key))
    assert f == a == (404, {"error": "not found"})


def test_decrypt_enclave_unset_503_parity(env, monkeypatch):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    # FEEDLING_ENCLAVE_URL not set (delenv in fixture).
    path = f"/v1/screen/frames/{fid}/decrypt"
    f = _flask_get(path, _key(api_key))
    a = _asgi("GET", path, headers=_key(api_key))
    assert f == a == (503, {"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"})


def test_decrypt_enclave_http_error_502_parity(env, monkeypatch):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    _install_enclave(monkeypatch, raise_http=True)
    path = f"/v1/screen/frames/{fid}/decrypt"
    f = _flask_get(path, _key(api_key))
    a = _asgi("GET", path, headers=_key(api_key))
    assert f[0] == a[0] == 502
    assert f[1] == a[1]
    assert f[1]["error"].startswith("enclave_error:")


# =========================================================================== #
# enclave image proxy — raw JPEG bytes + Range/streaming headers
# =========================================================================== #

def test_image_proxy_parity_headers_and_bytes(env, monkeypatch):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    jpeg = b"\xff\xd8\xff\xe0JPEGBYTES\xff\xd9"
    calls = _install_enclave(monkeypatch, content=jpeg, status=200, headers={
        "Content-Type": "image/jpeg",
        "Content-Length": str(len(jpeg)),
        "Accept-Ranges": "bytes",
        "ETag": '"abc"',
    })
    path = f"/v1/screen/frames/{fid}/image"
    f = _flask_get_raw(path, _key(api_key))
    a = _asgi_raw("GET", path, headers=_key(api_key))
    assert f[0] == a[0] == 200
    assert f[1] == a[1] == jpeg
    for h in ("content-type", "accept-ranges", "etag"):
        assert f[3].get(h) == a[3].get(h) == {
            "content-type": "image/jpeg",
            "accept-ranges": "bytes",
            "etag": '"abc"',
        }[h]
    # api key forwarded; no Range header added when the caller sent none.
    assert all("Range" not in c["headers"] for c in calls)
    assert all(c["headers"] == {"X-API-Key": api_key} for c in calls)


def test_image_forwards_range_and_returns_206(env, monkeypatch):
    uid, api_key = _register()
    fid, _ts, _env = _seed_frame(uid)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    part = b"PARTIAL"
    calls = _install_enclave(monkeypatch, content=part, status=206, headers={
        "Content-Type": "image/jpeg",
        "Content-Length": str(len(part)),
        "Content-Range": "bytes 0-6/100",
        "Accept-Ranges": "bytes",
    })
    headers = {**_key(api_key), "Range": "bytes=0-6"}
    path = f"/v1/screen/frames/{fid}/image"
    f = _flask_get_raw(path, headers)
    a = _asgi_raw("GET", path, headers)
    assert f[0] == a[0] == 206
    assert f[1] == a[1] == part
    assert f[3].get("content-range") == a[3].get("content-range") == "bytes 0-6/100"
    # The caller's Range was forwarded to the enclave on both backends.
    assert all(c["headers"].get("Range") == "bytes=0-6" for c in calls)


def test_image_frame_not_found_404_parity(env, monkeypatch):
    _uid, api_key = _register()
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    _install_enclave(monkeypatch)
    path = "/v1/screen/frames/deadbeefdeadbeef/image"
    f = _flask_get(path, _key(api_key))
    a = _asgi("GET", path, headers=_key(api_key))
    assert f == a == (404, {"error": "not found"})


# =========================================================================== #
# semantic analyze (JSON)
# =========================================================================== #

def test_analyze_no_frames_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/screen/analyze", _key(api_key))
    a = _asgi("GET", "/v1/screen/analyze", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["active"] is False
    assert f[1]["frame_count_in_window"] == 0


def test_analyze_with_frame_parity(env):
    uid, api_key = _register()
    _seed_frame(uid, app_name="com.apple.MobileSafari", ocr="")
    f = _flask_get("/v1/screen/analyze?window_sec=600", _key(api_key))
    a = _asgi("GET", "/v1/screen/analyze?window_sec=600", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["active"] is True
    assert f[1]["frame_count_in_window"] == 1
