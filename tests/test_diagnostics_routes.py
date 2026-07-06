from __future__ import annotations

import base64
import io
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from diagnostics import storage as diag_storage  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    # Ensure R2 is OFF so the route exercises the inline-Postgres fallback.
    for var in ("R2_ENDPOINT", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_USER_LOGS_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-test-token"}


def _upload(client, api_key, content: bytes, meta: dict | None = None):
    """POST a multipart file upload to the diagnostics endpoint."""
    data = {"file": (io.BytesIO(content), "diagnostics.log")}
    if meta is not None:
        data["meta"] = json.dumps(meta)
    return client.post(
        "/v1/diagnostics/logs",
        data=data,
        content_type="multipart/form-data",
        headers=_headers(api_key),
    )


def test_storage_disabled_without_env():
    assert diag_storage.enabled() is False


def test_upload_requires_auth(client):
    res = client.post(
        "/v1/diagnostics/logs",
        data={"file": (io.BytesIO(b"hi"), "diagnostics.log")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 401


def test_upload_rejects_missing_file(client):
    _, api_key = _register(client)
    res = client.post("/v1/diagnostics/logs", data={}, headers=_headers(api_key))
    assert res.status_code == 400


def test_upload_rejects_empty_file(client):
    _, api_key = _register(client)
    res = _upload(client, api_key, b"")
    assert res.status_code == 400


def test_upload_then_admin_read_roundtrip(client):
    uid, api_key = _register(client)
    res = _upload(client, api_key, "hello-log 测试 🚀".encode("utf-8"),
                  meta={"app_version": "1.2.3", "device": "iPhone"})
    assert res.status_code == 201, res.get_data(as_text=True)

    res = client.get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin_headers())
    assert res.status_code == 200
    body = res.get_json()
    assert body["user_id"] == uid
    assert len(body["logs"]) == 1
    entry = body["logs"][0]
    # No R2 configured → inline content, no download_url.
    assert entry["content"] == "hello-log 测试 🚀"
    assert entry["meta"]["app_version"] == "1.2.3"
    assert "download_url" not in entry


def test_content_truncated_to_512kb(client):
    uid, api_key = _register(client)
    res = _upload(client, api_key, b"a" * (600 * 1024))
    assert res.status_code == 201

    res = client.get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin_headers())
    entry = res.get_json()["logs"][0]
    assert len(entry["content"]) == 512 * 1024


def test_log_trim_keeps_newest_ten(client):
    uid, api_key = _register(client)
    for i in range(13):
        res = _upload(client, api_key, f"log-{i}".encode("utf-8"))
        assert res.status_code == 201

    res = client.get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin_headers())
    logs = res.get_json()["logs"]
    assert len(logs) == 10
    # Chronological order; newest 10 are log-3 .. log-12.
    assert logs[0]["content"] == "log-3"
    assert logs[-1]["content"] == "log-12"


def test_upload_rejects_oversized_body(client):
    _, api_key = _register(client)
    # >2 MiB request body — rejected from Content-Length before reading the file.
    res = _upload(client, api_key, b"x" * (2 * 1024 * 1024 + 1024))
    assert res.status_code == 413


def test_admin_read_requires_token(client):
    uid, _ = _register(client)
    res = client.get(f"/v1/admin/diagnostics/logs/{uid}")
    assert res.status_code == 401

    res = client.get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin_headers())
    assert res.status_code == 200
