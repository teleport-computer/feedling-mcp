"""POST /v1/model_api/runtime_error：agent-runner 路径补写 last_runtime_error。

历史教训（memory: model-api-providerkey-runtime-token-decrypt-gap）：Runtime V2
consumer 只有 runtime-token，读写侧只认 api-key 就会静默失效——这里走
require_auth（两者都收），测试只需覆盖 api-key 路径 + 语义。
Run:  python -m pytest tests/test_model_api_runtime_error_route.py -q
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from asgi_test_client import make_client  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(backend_env):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    uid, key = body["user_id"], body["api_key"]
    # record_runtime_error now writes the ACTIVE ROUTE row (post multi-profile
    # migration); GET /v1/model_api/runtime reads last_runtime_error(_class) off the
    # same route. Registering a bare user configures nothing, so seed an active route
    # here — otherwise runtime_error 404s with model_api_runtime_profile_missing
    # before the semantics under test (truncation / clearing) ever run.
    from conftest import configure_model_api_route
    configure_model_api_route(uid, provider="anthropic", model="claude-3-5-sonnet-latest")
    return uid, key


def _post(path, *, headers=None, json=None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(path, headers=headers or {}, json=json)
            return r.status_code, r.json()
    return asyncio.run(go())


def _get(path, *, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(path, headers=headers or {})
            return r.status_code, r.json()
    return asyncio.run(go())


def _runtime_profile(user_id):
    # record_runtime_error now stores last_runtime_error(_class) on the active route,
    # not the runtime profile blob — read it there.
    import db
    return db.model_api_active_route(user_id) or {}


def test_runtime_status_surfaces_error_class(user):
    """读侧 GET /v1/model_api/runtime 必须回带 last_runtime_error_class——
    iOS 设置页/SceneErrorCopy 靠它做结构化分类（契约 §六）；只回 error 文本
    会逼客户端从自由文本猜 slug。"""
    uid, key = user
    _post("/v1/model_api/runtime_error",
          headers={"X-API-Key": key},
          json={"error": "403 预扣费额度失败", "error_class": "quota_insufficient"})
    status, body = _get("/v1/model_api/runtime", headers={"X-API-Key": key})
    assert status == 200, body
    assert body["last_runtime_error"] == "403 预扣费额度失败"
    assert body["last_runtime_error_class"] == "quota_insufficient"


def test_report_and_clear_runtime_error(user):
    uid, key = user
    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "403 预扣费额度失败", "error_class": "quota_insufficient"})
    assert status == 200, body
    prof = _runtime_profile(uid)
    assert prof["last_runtime_error"] == "403 预扣费额度失败"

    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "", "error_class": ""})
    assert status == 200, body
    assert _runtime_profile(uid)["last_runtime_error"] == ""


def test_error_truncated_to_300(user):
    uid, key = user
    status, _ = _post("/v1/model_api/runtime_error",
                      headers={"X-API-Key": key},
                      json={"error": "x" * 900, "error_class": "k" * 200})
    assert status == 200
    prof = _runtime_profile(uid)
    assert len(prof["last_runtime_error"]) == 300
    assert len(prof["last_runtime_error_class"]) == 64


def test_bad_auth_401():
    status, _ = _post("/v1/model_api/runtime_error",
                      headers={"X-API-Key": "nope"}, json={"error": "e"})
    assert status == 401


def test_missing_profile_404(backend_env):
    """No model_api config configured yet -> record_runtime_error's 404 branch."""
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x22" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    key = res.get_json()["api_key"]

    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "boom", "error_class": "x"})
    assert status == 404, body
    assert body["error"] == "model_api_runtime_profile_missing"
