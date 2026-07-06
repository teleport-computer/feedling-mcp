"""Native /v1/onboarding/archive parity: multipart upload → R2 + Postgres index.

Asserts the FastAPI route (``onboarding_archive.routes_asgi``) returns the same
status/body semantics as the Flask oracle (``onboarding_archive.routes``) — both
call the same framework-neutral ``onboarding_archive_core``. R2 is forced ON with
a shared fake S3 client (the route has no inline-Postgres fallback), so no test
hits the network.

Parity note: the success body carries a random ``archive_id`` (hence a random
``key``), so two independent requests can never be byte-equal — success parity is
therefore *structural* (both 201, ``status=="ok"``, key prefix/suffix), while the
deterministic error paths (auth / missing / empty / oversized / R2-off /
R2-failure) are asserted byte-equal. The 413 body differs (Flask renders
Werkzeug's default HTML for ``max_content_length``; ASGI returns a JSON
``payload_too_large``), so 413 parity is asserted on *status only*.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from onboarding_archive import routes_asgi as arch_asgi  # noqa: E402


class _FakeS3:
    def __init__(self):
        self.store: dict[tuple, bytes] = {}

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.store[(Bucket, Key)] = Fileobj.read()


def _build_asgi_app() -> FastAPI:
    # Standalone app: the onboarding-archive router + the fixed-body exception
    # handlers, independent of asgi_app.py's package list (owned by the
    # orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    arch_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()
_pk_counter = itertools.count(1)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    # R2 ON (the route has no inline fallback) with a shared fake S3 client.
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_USER_LOGS_BUCKET", "io-user-logs")
    monkeypatch.setattr(object_storage, "_client", lambda: _FakeS3())
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


def _register() -> tuple[str, str]:
    import base64
    raw = next(_pk_counter).to_bytes(32, "big")
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode("ascii"), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# request helpers → (status, body) tuples
# --------------------------------------------------------------------------- #

def _flask_archive(api_key, content, *, filename="chat.json", extra=None):
    data = {"file": (io.BytesIO(content), filename), "filename": filename}
    if extra:
        data.update(extra)
    res = make_client().post(
        "/v1/onboarding/archive",
        data=data,
        content_type="multipart/form-data",
        headers=({"X-API-Key": api_key} if api_key else {}),
    )
    return res.status_code, res.get_json(silent=True)


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


def _asgi_archive(api_key, content, *, filename="chat.json", extra=None):
    files = {"file": (filename, content, "application/octet-stream")}
    data = {"filename": filename}
    if extra:
        data.update(extra)
    return _asgi(
        "POST", "/v1/onboarding/archive",
        headers=({"X-API-Key": api_key} if api_key else {}),
        files=files, data=data,
    )


def _key(api_key):
    return {"X-API-Key": api_key}


# --------------------------------------------------------------------------- #
# success (structural parity — random archive_id)
# --------------------------------------------------------------------------- #

def test_archive_success_parity(env):
    uid, api_key = _register()
    fs, fb = _flask_archive(
        api_key, b'{"x":1}', filename="chat export.json",
        extra={"content_type": "application/json", "client_job_id": "job-9"},
    )
    a_s, a_b = _asgi_archive(
        api_key, b'{"x":1}', filename="chat export.json",
        extra={"content_type": "application/json", "client_job_id": "job-9"},
    )
    assert fs == a_s == 201
    for body in (fb, a_b):
        assert body["status"] == "ok"
        assert body["archive_id"]
        # safe_filename collapses the space to "_"
        assert body["key"].startswith(f"onboarding/{uid}/{body['archive_id']}/")
        assert body["key"].endswith("/chat_export.json")


def test_archive_asgi_writes_r2_and_index(env):
    """The ASGI route itself streams to R2 and appends the untrimmed index row."""
    uid, api_key = _register()
    status, body = _asgi_archive(
        api_key, b'{"x":1}', filename="chat export.json",
        extra={"content_type": "application/json", "client_job_id": "job-9"},
    )
    assert status == 201
    rows = db.log_read(uid, "onboarding_archive", limit=10)
    assert len(rows) == 1
    assert rows[0]["filename"] == "chat export.json"
    assert rows[0]["client_job_id"] == "job-9"
    assert rows[0]["size_bytes"] == len(b'{"x":1}')
    assert rows[0]["r2_key"] == body["key"]


def test_archive_index_not_trimmed_parity(env):
    """Neither backend trims the index (52 > the old 50 cap → all kept)."""
    uid, api_key = _register()
    n = 52
    for i in range(n):
        assert _asgi_archive(api_key, b"x", filename=f"f{i}.json")[0] == 201
    assert len(db.log_read(uid, "onboarding_archive", limit=200)) == n


# --------------------------------------------------------------------------- #
# deterministic error paths (byte-equal parity)
# --------------------------------------------------------------------------- #

def test_requires_auth_parity(env):
    f = _flask_archive(None, b"x")
    a = _asgi_archive(None, b"x")
    assert f == a
    assert f == (401, {"error": "unauthorized"})


def test_missing_file_parity(env):
    _uid, api_key = _register()
    # No file part on either side (still a valid multipart with just a text field).
    fs = make_client().post(
        "/v1/onboarding/archive", data={"filename": "c.json"}, headers=_key(api_key)
    )
    f = (fs.status_code, fs.get_json(silent=True))
    a = _asgi("POST", "/v1/onboarding/archive", headers=_key(api_key), data={"filename": "c.json"})
    assert f == a
    assert f == (400, {"error": "missing_file"})


def test_empty_file_parity(env):
    _uid, api_key = _register()
    f = _flask_archive(api_key, b"")
    a = _asgi_archive(api_key, b"")
    assert f == a
    assert f == (400, {"error": "empty_file"})


def test_r2_unavailable_returns_503_parity(env, monkeypatch):
    _uid, api_key = _register()
    monkeypatch.delenv("R2_USER_LOGS_BUCKET", raising=False)
    f = _flask_archive(api_key, b"data")
    a = _asgi_archive(api_key, b"data")
    assert f == a
    assert f == (503, {"error": "archive_unavailable"})


def test_r2_put_failure_returns_502_parity(env, monkeypatch):
    _uid, api_key = _register()

    class _Boom:
        def upload_fileobj(self, *a, **k):
            raise RuntimeError("r2 down")

    monkeypatch.setattr(object_storage, "_client", lambda: _Boom())
    f = _flask_archive(api_key, b"data")
    a = _asgi_archive(api_key, b"data")
    assert f == a
    assert f == (502, {"error": "archive_failed"})


# --------------------------------------------------------------------------- #
# oversized (status-only parity — the 413 body shape differs across backends)
# --------------------------------------------------------------------------- #

def test_oversized_body_returns_413_status_parity(env):
    _uid, api_key = _register()
    big = b"x" * (25 * 1024 * 1024 + 1024)
    fs, _fb = _flask_archive(api_key, big)
    a_s, _ab = _asgi_archive(api_key, big)
    assert fs == 413
    assert a_s == 413


def test_archive_oversized_chunked_no_content_length_is_413(env, monkeypatch):
    """A chunked upload with NO Content-Length (or a lying one) must still be
    rejected 413 before it streams to R2 — the ASGI parity for Flask's
    reject-oversized-during-read (codex-review finding). We enforce this on the
    ACTUAL spooled size, so it fires even when the up-front Content-Length check
    can't. Cap is shrunk so the test body stays tiny."""
    from onboarding_archive import onboarding_archive_core as arch_core

    monkeypatch.setattr(arch_core, "_MAX_REQUEST_BYTES", 100)
    _uid, api_key = _register()

    boundary = "----obtest"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.json"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode("latin-1")
    tail = f"\r\n--{boundary}--\r\n".encode("latin-1")
    payload = head + (b"x" * 500) + tail  # well over the shrunk 100-byte cap

    async def _gen():
        # Yield in chunks → httpx uses chunked transfer-encoding (no Content-Length).
        for i in range(0, len(payload), 64):
            yield payload[i : i + 64]

    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(
                "/v1/onboarding/archive",
                content=_gen(),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "X-API-Key": api_key,
                },
            )
            return resp.status_code

    assert asyncio.run(go()) == 413
