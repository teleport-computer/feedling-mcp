"""Route tests for POST /v1/onboarding/archive.

R2 is forced ON with a fake S3 client (the route has no inline-PG fallback).
"""

import io
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


class _FakeS3:
    def __init__(self):
        self.store: dict[tuple, bytes] = {}

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.store[(Bucket, Key)] = Fileobj.read()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_USER_LOGS_BUCKET", "io-user-logs")
    monkeypatch.setattr(object_storage, "_client", lambda: _FakeS3())
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    import base64
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode(), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _archive(client, api_key, content: bytes, filename="chat.json", extra=None):
    data = {"file": (io.BytesIO(content), filename), "filename": filename}
    if extra:
        data.update(extra)
    return client.post(
        "/v1/onboarding/archive",
        data=data,
        content_type="multipart/form-data",
        headers={"X-API-Key": api_key},
    )


def test_requires_auth(client):
    res = client.post("/v1/onboarding/archive",
                      data={"file": (io.BytesIO(b"x"), "c.json"), "filename": "c.json"},
                      content_type="multipart/form-data")
    assert res.status_code == 401


def test_rejects_missing_file(client):
    _, api_key = _register(client)
    res = client.post("/v1/onboarding/archive", data={"filename": "c.json"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 400


def test_rejects_empty_file(client):
    _, api_key = _register(client)
    res = _archive(client, api_key, b"")
    assert res.status_code == 400


def test_archive_success_writes_r2_and_index(client):
    uid, api_key = _register(client)
    res = _archive(client, api_key, b'{"x":1}', filename="chat export.json",
                   extra={"content_type": "application/json", "client_job_id": "job-9"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["archive_id"]
    # safe_filename 把空格清成 _
    assert body["key"].startswith(f"onboarding/{uid}/{body['archive_id']}/")
    assert body["key"].endswith("/chat_export.json")
    rows = db.log_read(uid, "onboarding_archive", limit=10)
    assert len(rows) == 1
    assert rows[0]["filename"] == "chat export.json"
    assert rows[0]["client_job_id"] == "job-9"
    assert rows[0]["size_bytes"] == len(b'{"x":1}')


def test_r2_unavailable_returns_503(client, monkeypatch):
    _, api_key = _register(client)
    monkeypatch.delenv("R2_USER_LOGS_BUCKET", raising=False)
    res = _archive(client, api_key, b"data")
    assert res.status_code == 503


def test_r2_put_failure_returns_502(client, monkeypatch):
    _, api_key = _register(client)

    class _Boom:
        def upload_fileobj(self, *a, **k):
            raise RuntimeError("r2 down")

    monkeypatch.setattr(object_storage, "_client", lambda: _Boom())
    res = _archive(client, api_key, b"data")
    assert res.status_code == 502


def test_oversized_body_returns_413(client):
    _, api_key = _register(client)
    res = _archive(client, api_key, b"x" * (25 * 1024 * 1024 + 1024))
    assert res.status_code == 413


def test_index_not_trimmed_keeps_all_rows(client):
    uid, api_key = _register(client)
    n = 52
    for i in range(n):
        res = _archive(client, api_key, b"x", filename=f"f{i}.json")
        assert res.status_code == 201, res.get_data(as_text=True)
    rows = db.log_read(uid, "onboarding_archive", limit=200)
    assert len(rows) == n  # 52 > 旧的 50 上限；全部保留，未被 trim


def test_oversized_without_content_length_still_413(client):
    """无 Content-Length 的超大请求也必须被拦截（P2 修复）。

    构造方式：CONTENT_LENGTH=None（使 request.content_length is None）+
    wsgi.input_terminated=True（模拟服务端分块流，让 werkzeug 读实际流并按字节计数）。
    request.max_content_length 在解析期强制，超限抛 RequestEntityTooLarge → 413。
    """
    _, api_key = _register(client)
    big = b"x" * (25 * 1024 * 1024 + 1024)
    res = client.post(
        "/v1/onboarding/archive",
        data={"file": (io.BytesIO(big), "chat.json"), "filename": "chat.json"},
        content_type="multipart/form-data",
        headers={"X-API-Key": api_key},
        environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True},
    )
    assert res.status_code == 413, res.get_data(as_text=True)
